import os
from pathlib import Path

import pytest

from jarvis.security.permissions import SecurityPolicy
from jarvis.tools.files import (
    BulkMoveFilesTool,
    CreateDirectoryTool,
    ListDirectoryTool,
    PermanentlyDeleteFilesTool,
    ReadTextFileTool,
    SearchFilesTool,
    TrashFileTool,
)


@pytest.fixture
def populated(sandbox: Path) -> Path:
    (sandbox / "docs").mkdir()
    (sandbox / "docs" / "report.pdf").write_bytes(b"%PDF")
    (sandbox / "docs" / "Invoice_2026.PDF").write_bytes(b"%PDF")
    (sandbox / "notes.txt").write_text("hello world")
    (sandbox / ".hidden").write_text("x")
    (sandbox / "secret").mkdir()
    (sandbox / "secret" / "plans.pdf").write_bytes(b"%PDF")
    return sandbox


# --- list_directory --------------------------------------------------------------------


async def test_list_directory(populated: Path, policy: SecurityPolicy):
    tool = ListDirectoryTool(policy)
    result = await tool.execute("c1", directory_path=str(populated))
    assert result.success
    names = [i["name"] for i in result.data["items"]]
    assert names == ["docs", "notes.txt", "secret"]
    notes = next(i for i in result.data["items"] if i["name"] == "notes.txt")
    assert notes["size_bytes"] == 11 and notes["type"] == "file"
    docs = next(i for i in result.data["items"] if i["name"] == "docs")
    assert docs["type"] == "folder" and docs["item_count"] == 2
    assert await tool.verify(result)


async def test_list_directory_show_hidden(populated: Path, policy: SecurityPolicy):
    result = await ListDirectoryTool(policy).execute(
        "c1", directory_path=str(populated), show_hidden=True
    )
    assert ".hidden" in [i["name"] for i in result.data["items"]]


async def test_list_directory_denied(tmp_path: Path, policy: SecurityPolicy):
    result = await ListDirectoryTool(policy).execute("c1", directory_path=str(tmp_path / "outside"))
    assert not result.success and "Access denied" in result.error
    assert not await ListDirectoryTool(policy).verify(result)


async def test_list_directory_missing(sandbox: Path, policy: SecurityPolicy):
    result = await ListDirectoryTool(policy).execute("c1", directory_path=str(sandbox / "nope"))
    assert not result.success and "Not a directory" in result.error


# --- search_files ----------------------------------------------------------------------


async def test_search_files_case_insensitive_and_skips_denied(
    populated: Path, policy: SecurityPolicy
):
    result = await SearchFilesTool(policy).execute(
        "c1", directory_path=str(populated), pattern="*.pdf"
    )
    assert result.success
    found = sorted(Path(p).name for p in result.data["matches"])
    assert found == ["Invoice_2026.PDF", "report.pdf"]  # secret/plans.pdf is denied


async def test_search_files_non_recursive(populated: Path, policy: SecurityPolicy):
    result = await SearchFilesTool(policy).execute(
        "c1", directory_path=str(populated), pattern="*.pdf", recursive=False
    )
    assert result.data["matches"] == []


async def test_search_files_max_results(populated: Path, policy: SecurityPolicy):
    result = await SearchFilesTool(policy).execute(
        "c1", directory_path=str(populated), pattern="*", max_results=2
    )
    assert len(result.data["matches"]) == 2 and result.data["truncated"] is True


# --- read_text_file --------------------------------------------------------------------


async def test_read_text_file(populated: Path, policy: SecurityPolicy):
    result = await ReadTextFileTool(policy).execute("c1", file_path=str(populated / "notes.txt"))
    assert result.success and result.data["content"] == "hello world"
    assert result.data["truncated"] is False


async def test_read_text_file_truncates(populated: Path, policy: SecurityPolicy):
    result = await ReadTextFileTool(policy).execute(
        "c1", file_path=str(populated / "notes.txt"), max_bytes=5
    )
    assert result.data["content"] == "hello" and result.data["truncated"] is True


async def test_read_text_file_rejects_binary(sandbox: Path, policy: SecurityPolicy):
    (sandbox / "blob.bin").write_bytes(b"ab\x00cd")
    result = await ReadTextFileTool(policy).execute("c1", file_path=str(sandbox / "blob.bin"))
    assert not result.success and "binary" in result.error


async def test_read_text_file_via_symlink_escape_denied(
    sandbox: Path, policy: SecurityPolicy, tmp_path: Path
):
    secret = tmp_path / "outside" / "pw.txt"
    secret.write_text("hunter2")
    os.symlink(secret, sandbox / "link.txt")
    result = await ReadTextFileTool(policy).execute("c1", file_path=str(sandbox / "link.txt"))
    assert not result.success and "Access denied" in result.error


# --- create_directory ------------------------------------------------------------------


async def test_create_directory(sandbox: Path, policy: SecurityPolicy):
    tool = CreateDirectoryTool(policy)
    result = await tool.execute("c1", directory_path=str(sandbox / "a" / "b"))
    assert result.success and (sandbox / "a" / "b").is_dir()
    assert result.data["already_existed"] is False
    assert await tool.verify(result)


async def test_create_directory_verify_detects_missing(sandbox: Path, policy: SecurityPolicy):
    tool = CreateDirectoryTool(policy)
    result = await tool.execute("c1", directory_path=str(sandbox / "gone"))
    (sandbox / "gone").rmdir()
    assert not await tool.verify(result)


# --- trash_file ------------------------------------------------------------------------


async def test_trash_file(populated: Path, policy: SecurityPolicy, tmp_path: Path):
    trash = tmp_path / "Trash"
    tool = TrashFileTool(policy, trash)
    result = await tool.execute("c1", path=str(populated / "notes.txt"))
    assert result.success, result.error
    assert not (populated / "notes.txt").exists()
    assert (trash / "files" / "notes.txt").read_text() == "hello world"
    info = (trash / "info" / "notes.txt.trashinfo").read_text()
    assert info.startswith("[Trash Info]\nPath=") and "DeletionDate=" in info
    assert await tool.verify(result)


async def test_trash_file_name_collision(sandbox: Path, policy: SecurityPolicy, tmp_path: Path):
    tool = TrashFileTool(policy, tmp_path / "Trash")
    for _ in range(2):
        (sandbox / "a.txt").write_text("x")
        assert (await tool.execute("c1", path=str(sandbox / "a.txt"))).success
    assert (tmp_path / "Trash" / "files" / "a.2.txt").exists()


async def test_trash_refuses_sandbox_root(sandbox: Path, policy: SecurityPolicy, tmp_path: Path):
    result = await TrashFileTool(policy, tmp_path / "Trash").execute("c1", path=str(sandbox))
    assert not result.success and "sandbox root" in result.error
    assert sandbox.is_dir()


# --- bulk_move_files -------------------------------------------------------------------


async def test_bulk_move(populated: Path, policy: SecurityPolicy):
    tool = BulkMoveFilesTool(policy)
    result = await tool.execute(
        "c1",
        source_paths=[str(populated / "notes.txt"), str(populated / "docs" / "report.pdf")],
        destination_directory=str(populated / "docs"),
    )
    # report.pdf already lives in docs -> refusing to overwrite itself
    assert not result.success and "overwrite" in result.error
    assert (populated / "docs" / "notes.txt").exists()  # the valid move still happened
    assert len(result.data["moved"]) == 1


async def test_bulk_move_success_and_verify(populated: Path, policy: SecurityPolicy):
    (populated / "archive").mkdir()
    tool = BulkMoveFilesTool(policy)
    result = await tool.execute(
        "c1", source_paths=[str(populated / "notes.txt")],
        destination_directory=str(populated / "archive"),
    )
    assert result.success and await tool.verify(result)


async def test_bulk_move_into_itself(populated: Path, policy: SecurityPolicy):
    result = await BulkMoveFilesTool(policy).execute(
        "c1", source_paths=[str(populated / "docs")],
        destination_directory=str(populated / "docs"),
    )
    assert not result.success and (populated / "docs").is_dir()


async def test_bulk_move_outside_denied(populated: Path, policy: SecurityPolicy, tmp_path: Path):
    result = await BulkMoveFilesTool(policy).execute(
        "c1", source_paths=[str(populated / "notes.txt")],
        destination_directory=str(tmp_path / "outside"),
    )
    assert not result.success and (populated / "notes.txt").exists()


# --- permanently_delete_files ----------------------------------------------------------


async def test_permanent_delete(populated: Path, policy: SecurityPolicy):
    tool = PermanentlyDeleteFilesTool(policy)
    result = await tool.execute("c1", paths=[str(populated / "notes.txt")])
    assert result.success and not (populated / "notes.txt").exists()
    assert await tool.verify(result)


async def test_permanent_delete_nonempty_dir_requires_recursive(
    populated: Path, policy: SecurityPolicy
):
    tool = PermanentlyDeleteFilesTool(policy)
    result = await tool.execute("c1", paths=[str(populated / "docs")])
    assert not result.success and (populated / "docs").is_dir()
    result = await tool.execute("c1", paths=[str(populated / "docs")], recursive=True)
    assert result.success and not (populated / "docs").exists()


async def test_permanent_delete_symlink_removes_link_only(sandbox: Path, policy: SecurityPolicy):
    target = sandbox / "real.txt"
    target.write_text("keep me")
    os.symlink(target, sandbox / "link.txt")
    result = await PermanentlyDeleteFilesTool(policy).execute(
        "c1", paths=[str(sandbox / "link.txt")]
    )
    assert result.success
    assert not (sandbox / "link.txt").is_symlink() and target.read_text() == "keep me"


async def test_permanent_delete_refuses_root(sandbox: Path, policy: SecurityPolicy):
    result = await PermanentlyDeleteFilesTool(policy).execute(
        "c1", paths=[str(sandbox)], recursive=True
    )
    assert not result.success and sandbox.is_dir()
