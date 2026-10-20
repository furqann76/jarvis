"""Screen understanding with a local vision-language model (Qwen2.5-VL via Ollama)."""

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage

from jarvis.models.ollama_client import OllamaError

PATCH = 28  # Qwen2.5-VL works on 28px patches; sizing to multiples keeps coords exact
_JSON_BLOCK = re.compile(r"\[.*\]|\{.*\}", re.DOTALL)


@dataclass
class PreparedImage:
    b64: str
    width: int  # size the model sees
    height: int
    source_width: int  # size of the original screenshot
    source_height: int

    def to_source(self, x: float, y: float) -> tuple[float, float]:
        return x * self.source_width / self.width, y * self.source_height / self.height


def prepare_image(path: str | Path, max_width: int = 1288) -> PreparedImage:
    img = QImage(str(path))
    if img.isNull():
        raise OllamaError(f"Could not read screenshot {path}")
    scale = min(1.0, max_width / img.width())
    w = max(PATCH, round(img.width() * scale / PATCH) * PATCH)
    h = max(PATCH, round(img.height() * scale / PATCH) * PATCH)
    scaled = img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    scaled.save(buf, "JPEG", 90)
    return PreparedImage(base64.b64encode(bytes(data)).decode(), w, h, img.width(), img.height())


def parse_bbox(text: str) -> tuple[float, float, float, float] | None:
    """Extract the first bbox_2d [x1, y1, x2, y2] (or a point) from a model reply."""
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        nums = re.findall(r"-?\d+(?:\.\d+)?", match.group(0))
        return tuple(map(float, nums[:4])) if len(nums) >= 4 else None
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        box = item.get("bbox_2d") or item.get("bbox")
        if isinstance(box, list) and len(box) == 4:
            return tuple(float(v) for v in box)
        point = item.get("point_2d") or item.get("point")
        if isinstance(point, list) and len(point) == 2:
            x, y = map(float, point)
            return x, y, x, y
    return None


class VisionModel:
    def __init__(self, host: str, model: str, timeout_s: float = 120.0):
        self.model = model
        self._client = httpx.AsyncClient(base_url=host, timeout=timeout_s)

    async def _ask(self, image: PreparedImage, prompt: str) -> str:
        try:
            resp = await self._client.post("/api/chat", json={
                "model": self.model,
                "stream": False,
                "options": {"temperature": 0},
                "messages": [{"role": "user", "content": prompt, "images": [image.b64]}],
            })
        except httpx.HTTPError as e:
            raise OllamaError(f"Vision model request failed: {e}") from e
        if resp.status_code != 200:
            raise OllamaError(f"Vision model error {resp.status_code}: {resp.text[:300]}. "
                              f"Is '{self.model}' pulled? (ollama pull {self.model})")
        return resp.json().get("message", {}).get("content", "").strip()

    async def describe(self, screenshot: Path, question: str | None) -> str:
        image = prepare_image(screenshot)
        prompt = question or (
            "Describe what is on this screen: which application windows are open, what is "
            "in focus, and any important text, dialogs or buttons."
        )
        return await self._ask(image, prompt)

    async def locate(self, screenshot: Path, target: str) -> tuple[float, float] | None:
        """Centre of `target` in the screenshot's pixel coordinates, or None."""
        image = prepare_image(screenshot)
        reply = await self._ask(
            image,
            f'Locate "{target}" in this screenshot and output its bbox coordinates in JSON '
            f'format like [{{"bbox_2d": [x1, y1, x2, y2], "label": "..."}}]. '
            f'If it is not visible, output [].',
        )
        box = parse_bbox(reply)
        if box is None:
            return None
        x1, y1, x2, y2 = box
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        if not (0 <= cx <= image.width and 0 <= cy <= image.height):
            return None
        return image.to_source(cx, cy)

    async def aclose(self) -> None:
        await self._client.aclose()
