# Technical Specification & System Architecture Blueprint: Jarvis Desktop AI Agent

## 1. System Overview & Technical Goals

**Jarvis** is a standalone, local-first computer-use assistant built for **Ubuntu 24.04 LTS**. It translates natural language input (spoken or typed) into structured tool-execution plans to interact with the host system (filesystem, applications, accessibility interfaces, and web browsers) while running **100% locally with zero paid API dependencies**.

### Hardware & Operating Context Target
* **CPU:** Intel Core i7-12700
* **GPU:** NVIDIA GeForce RTX 4060 Ti (16 GB VRAM)
* **OS:** Ubuntu 24.04 LTS (Desktop, GNOME, Wayland/X11 hybrid context)
* **Primary Constraints:** Local execution only, explicit system sandboxing per user policy, single-process PySide6 event loop architecture with offloaded async workers.

---

## 2. Component Architecture & System Topography

Jarvis uses a single desktop application process written in Python 3.12+, utilizing PySide6 signals/slots to isolate heavy computational workers (LLM inference, Whisper STT, Piper TTS) from the Qt main thread.

```
+-----------------------------------------------------------------------------------+
|                                 PYSIDE6 DESKTOP UI                                |
|  +--------------------+  +--------------------+  +-----------------------------+  |
|  | Voice Input / STT  |  | Chat & Command     |  | Task Timeline & Visualizer  |  |
|  +---------+----------+  +---------+----------+  +--------------+--------------+  |
+------------|-----------------------|--------------------------|-------------------+
             | (Speech text)         | (Intent text)            | (Task state updates)
             v                       v                          |
+---------------------------------------------------------------+-------------------+
|                             AGENT ORCHESTRATOR                |                   |
|  +------------------------------------------------------------+-----------------+  |
|  | Task State Machine & Planning Engine (ReAct / Structured Loop)               |  |
|  +-------------------------------------+----------------------------------------+  |
+----------------------------------------|------------------------------------------+
                                         | Query / Tool Schema Request
                                         v
+-----------------------------------------------------------------------------------+
|                              LOCAL MODEL RUNTIMES                                 |
|  +---------------------------------+  +----------------------------------------+  |
|  | Ollama (LLM Engine - Qwen3 8B)   |  | Speech (faster-whisper / Piper TTS)    |  |
|  +---------------------------------+  +----------------------------------------+  |
+----------------------------------------|------------------------------------------+
                                         | Structured Tool Invocation
                                         v
+-----------------------------------------------------------------------------------+
|                            VALIDATED TOOL REGISTRY                                |
|  +---------------+  +---------------+  +-------------------+  +-----------------+  |
|  |  Filesystem   |  | Applications  |  | Desktop (AT-SPI)  |  |   Playwright    |  |
|  +-------+-------+  +-------+-------+  +---------+---------+  +--------+--------+  |
+----------|------------------|--------------------+-------------------|------------+
           v                  v                    v                   v
+-----------------------------------------------------------------------------------+
|                        SECURITY & AUDIT BOUNDARY                                  |
|  [Path Validator]  [Permission Enforcer]  [Interactive Approver]  [SQLite Audit Log]|
+-----------------------------------------------------------------------------------+
```

---

## 3. Technology Stack & Runtime Dependencies

| Domain | Technology | Configuration / Version | Notes |
| :--- | :--- | :--- | :--- |
| **Desktop UI** | PySide6 | `~=6.7` | Official Qt bindings; native GNOME integration |
| **Local Inference** | Ollama | System Daemon (`v0.3+`) | REST / HTTP client communication |
| **Primary LLM** | Qwen3-8B-Instruct (or Qwen2.5-7B) | `q4_k_m` or `q5_k_m` GGUF | Supports OpenAI tool-calling spec via JSON Schema |
| **Speech-to-Text** | `faster-whisper` | `medium.en` / `small.en` | CTranslate2 GPU engine, CUDA 12.x backend |
| **Text-to-Speech** | `piper-tts` | ONNX local voice models | Low latency CPU execution |
| **Browser Engine** | Playwright for Python | Headed / Headless Chromium | DOM-aware accessibility tree traversal |
| **Desktop Control** | PyATSPI / D-Bus | Native Ubuntu AT-SPI2 | Window introspection, semantic control |
| **Input Fallback** | `PyAutoGUI` / `python-xlib` | Coordinate fallback | Used only when AT-SPI node activation fails |
| **State & Persistence**| SQLite + Pydantic v2 | Python `sqlite3` + `pydantic` | WAL mode, audit trail logging, settings storage |

---

## 4. Execution Lifecycle & Task State Machine

Every user request initiates a formal `Task` governed by a deterministic state machine. **The LLM cannot directly mutate state without passing through the Security Policy layer.**

```
       +--------------+
       |   RECEIVED   |
       +-------+------+
               |
               v
       +--------------+
       |   PLANNING   |<-----------------------+
       +-------+------+                        |
               | (Tool Request)                |
               v                               |
    [Requires Approval?]                       |
       /            \                          |
     (Yes)          (No)                       |
     /                \                        |
    v                  v                       |
+-------+      +---------------+               |
|AWAITING|     |   EXECUTING   |               |
|APPROVAL|     +-------+-------+               |
+---+---+              |                       |
    | (Approved)       v                       |
    |          +---------------+               |
    +--------->|   VERIFYING   |               |
               +-------+-------+               |
                       |                       |
               [Check Succeeded?]              |
                 /           \                 |
              (Yes)          (No - Safe Retry) |
               /               +---------------+
              v
       +---------------+
       |   COMPLETED   |
       +---------------+
```

### State Definitions
1. **RECEIVED:** Command parsed from Voice (STT) or Text input.
2. **PLANNING:** LLM processes prompt + tool context to generate intent and structured tool call(s).
3. **AWAITING_APPROVAL:** Triggered if a tool call requires explicit user authorization (e.g., destructive file moves, browser submissions, system state changes).
4. **EXECUTING:** The system invokes the validated Python implementation of the target tool.
5. **VERIFYING:** A post-condition inspection function runs (e.g., checking if `file_exists` or window state updated) to confirm execution success.
6. **COMPLETED:** Goal state verified; final voice/text response returned to the user interface.
7. **FAILED / CANCELLED:** Operation halted due to validation failure, user denial, or unrecoverable error.

---

## 5. Security Architecture & Permission Framework

Jarvis operates strictly under user permissions without elevated privileges (`sudo` access is disabled by default).

### Tool Risk Tiers
* **Tier 0 (Read-Only / Safe):** No permission required.
  * `list_directory`, `search_files`, `get_active_window`, `inspect_dom`
* **Tier 1 (Reversible Mutations):** Low-friction execution; notify in UI.
  * `create_directory`, `open_browser_url`, `launch_application`, `trash_file`
* **Tier 2 (High-Impact / Non-Reversible):** Explicit interactive user approval required via PySide6 modal dialog.
  * `permanently_delete_files`, `bulk_move_files`, `submit_web_form`, `execute_custom_script`

### Sandbox Directory Constraints
By default, filesystem tool activity is constrained to a whitelist configured in `settings.toml`:
```toml
[permissions.filesystem]
allowed_paths = [
    "~/Downloads",
    "~/Documents",
    "~/Desktop",
    "/tmp/jarvis_workspace"
]
deny_patterns = [
    "~/.ssh/*",
    "~/.bashrc",
    "/etc/*",
    "/usr/*"
]
```

---

## 6. Repository Scaffold & Directory Structure

Below is the directory map for the project:

```
jarvis-desktop/
├── pyproject.toml
├── README.md
├── .gitignore
├── config/
│   └── settings.toml
├── src/
│   └── jarvis/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── orchestrator.py
│       │   ├── planner.py
│       │   ├── task_state.py
│       │   └── tool_registry.py
│       ├── ui/
│       │   ├── __init__.py
│       │   ├── main_window.py
│       │   ├── chat_view.py
│       │   ├── task_timeline.py
│       │   ├── approval_dialog.py
│       │   └── components/
│       ├── models/
│       │   ├── __init__.py
│       │   ├── ollama_client.py
│       │   └── schemas.py
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── files.py
│       │   ├── applications.py
│       │   ├── desktop.py
│       │   └── browser.py
│       ├── security/
│       │   ├── __init__.py
│       │   ├── permissions.py
│       │   ├── validation.py
│       │   └── audit.py
│       ├── voice/
│       │   ├── __init__.py
│       │   ├── microphone.py
│       │   ├── stt.py
│       │   └── tts.py
│       └── storage/
│           ├── __init__.py
│           ├── database.py
│           └── repository.py
├── tests/
│   ├── unit/
│   │   ├── test_security.py
│   │   ├── test_tools.py
│   │   └── test_state_machine.py
│   ├── integration/
│   │   └── test_ollama_tools.py
│   └── e2e/
└── scripts/
    └── setup_env.sh
```

---

## 7. Phase 1 Core Implementation Specification

The initial implementation sprint delivers a functional, tested text-based local agent executing validated filesystem tools.

### 7.1 Data Schemas (`src/jarvis/models/schemas.py`)

```python
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class TaskStatus(str, Enum):
    RECEIVED = "RECEIVED"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class RiskLevel(str, Enum):
    SAFE = "SAFE"
    LOW = "LOW"
    HIGH = "HIGH"

class ToolCallRequest(BaseModel):
    id: str
    name: str
    arguments: Dict[str, Any]

class ToolResult(BaseModel):
    tool_call_id: str
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    verification_passed: bool = False

class TaskStep(BaseModel):
    step_id: int
    description: str
    tool_call: Optional[ToolCallRequest] = None
    status: TaskStatus = TaskStatus.RECEIVED
    result: Optional[ToolResult] = None

class Task(BaseModel):
    id: str
    user_prompt: str
    status: TaskStatus = TaskStatus.RECEIVED
    steps: List[TaskStep] = []
    error_message: Optional[str] = None
```

### 7.2 Tool Base Interface (`src/jarvis/tools/base.py`)

```python
from abc import ABC, abstractmethod
from typing import Any, Dict
from jarvis.models.schemas import RiskLevel, ToolResult

class BaseTool(ABC):
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema definition for Ollama
    risk_level: RiskLevel = RiskLevel.SAFE

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """Executes the tool logic with provided validated arguments."""
        pass

    @abstractmethod
    async def verify(self, execution_result: ToolResult) -> bool:
        """Verifies if the action achieved its intended state on the system."""
        pass
```

### 7.3 File Tools Implementation Example (`src/jarvis/tools/files.py`)

```python
import os
from pathlib import Path
from typing import Any, Dict
from jarvis.tools.base import BaseTool
from jarvis.models.schemas import RiskLevel, ToolResult
from jarvis.security.permissions import SecurityPolicy

class ListFilesTool(BaseTool):
    name = "list_files"
    description = "List files and directories within an allowed folder path."
    risk_level = RiskLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {
            "directory_path": {
                "type": "string",
                "description": "Absolute or home-relative path to inspect."
            }
        },
        "required": ["directory_path"]
    }

    def __init__(self, security_policy: SecurityPolicy):
        self.security = security_policy

    async def execute(self, tool_call_id: str, directory_path: str) -> ToolResult:
        resolved_path = Path(os.path.expanduser(directory_path)).resolve()
        
        if not self.security.is_path_allowed(resolved_path):
            return ToolResult(
                tool_call_id=tool_call_id,
                success=False,
                error=f"Access denied to path: {resolved_path}"
            )

        try:
            entries = os.listdir(resolved_path)
            items = []
            for entry in entries:
                full = resolved_path / entry
                items.append({
                    "name": entry,
                    "is_dir": full.is_dir(),
                    "size_bytes": full.stat().st_size if full.is_file() else 0
                })
            return ToolResult(
                tool_call_id=tool_call_id,
                success=True,
                data={"path": str(resolved_path), "items": items}
            )
        except Exception as e:
            return ToolResult(tool_call_id=tool_call_id, success=False, error=str(e))

    async def verify(self, execution_result: ToolResult) -> bool:
        # Read-only operation verification
        return execution_result.success
```

---

## 8. Verification & Test Strategy

Jarvis utilizes `pytest` and `pytest-asyncio` for test-driven execution assurance.

### Critical Safety Test (`tests/unit/test_security.py`)
```python
import pytest
from pathlib import Path
from jarvis.security.permissions import SecurityPolicy

def test_unauthorized_directory_traversal():
    policy = SecurityPolicy(allowed_paths=["~/Downloads"])
    
    # Attempt unauthorized access
    unauthorized_path = Path("/etc/passwd").resolve()
    traversal_path = Path("~/Downloads/../../etc/shadow").resolve()
    
    assert policy.is_path_allowed(unauthorized_path) is False
    assert policy.is_path_allowed(traversal_path) is False

def test_authorized_directory_access():
    policy = SecurityPolicy(allowed_paths=["~/Downloads"])
    authorized_path = Path("~/Downloads/my_file.txt").expanduser().resolve()
    
    assert policy.is_path_allowed(authorized_path) is True
```

---

## 9. Next Steps for Implementation

1. **Environment Setup:** Execute `scripts/setup_env.sh` to initialize Python 3.12 venv, CUDA environment, and install PySide6.
2. **Model Download:** Pull target model via Ollama: `ollama pull qwen2.5:7b-instruct` or `ollama pull qwen3:8b`.
3. **Run Sprint 1:** Launch UI via `python -m jarvis.main` to verify the task timeline, Ollama local tool registration, and execution loop against harmless directory read commands.