import os
from pathlib import Path

import pytest

from jarvis.models.schemas import RiskLevel
from jarvis.security.permissions import SecurityPolicy
from jarvis.security.validation import ValidationError, validate_tool_call
from jarvis.tools.files import BulkMoveFilesTool, ListDirectoryTool

# --- Tests from the specification (§8) ----------------------------------------------


def test_unauthorized_directory_traversal():
    policy = SecurityPolicy(allowed_paths=["~/Downloads"])

    unauthorized_path = Path("/etc/passwd").resolve()
    traversal_path = Path("~/Downloads/../../etc/shadow").resolve()

    assert policy.is_path_allowed(unauthorized_path) is False
    assert policy.is_path_allowed(traversal_path) is False


def test_authorized_directory_access():
    policy = SecurityPolicy(allowed_paths=["~/Downloads"])
    authorized_path = Path("~/Downloads/my_file.txt").expanduser().resolve()

    assert policy.is_path_allowed(authorized_path) is True


# --- Additional sandbox coverage -------------------------------------------------------


def test_expanded_home_traversal_is_denied():
    policy = SecurityPolicy(allowed_paths=["~/Downloads"])
    assert policy.is_path_allowed("~/Downloads/../.ssh/id_rsa") is False
    assert policy.is_path_allowed(Path("~/Downloads/../../../etc/shadow").expanduser()) is False


def test_policy_normalizes_raw_strings(sandbox: Path, policy: SecurityPolicy):
    assert policy.is_path_allowed(f"{sandbox}/a/../b.txt") is True
    assert policy.is_path_allowed(f"{sandbox}/../outside/x") is False


def test_root_itself_is_allowed(sandbox: Path, policy: SecurityPolicy):
    assert policy.is_path_allowed(sandbox) is True


def test_sibling_with_common_prefix_is_denied(sandbox: Path, policy: SecurityPolicy):
    sibling = sandbox.parent / (sandbox.name + "_evil")
    assert policy.is_path_allowed(sibling / "file") is False


def test_deny_patterns_override_allowlist(sandbox: Path, policy: SecurityPolicy):
    assert policy.is_path_allowed(sandbox / "secret" / "x.txt") is False
    assert policy.is_path_allowed(sandbox / "secret" / "deep" / "y") is False
    assert policy.is_path_allowed(sandbox / "secret") is False  # "dir/*" covers dir itself
    assert policy.is_path_allowed(sandbox / "server.key") is False
    assert policy.is_path_allowed(sandbox / "public.txt") is True


def test_default_deny_patterns():
    policy = SecurityPolicy(
        allowed_paths=["~", "/"], deny_patterns=["~/.ssh/*", "~/.bashrc", "/etc/*", "/usr/*"]
    )
    assert policy.is_path_allowed("~/.ssh/id_ed25519") is False
    assert policy.is_path_allowed("~/.ssh") is False
    assert policy.is_path_allowed("~/.bashrc") is False
    assert policy.is_path_allowed("/etc/passwd") is False
    assert policy.is_path_allowed("/usr/bin/python3") is False


def test_symlink_escape_is_denied(sandbox: Path, policy: SecurityPolicy, tmp_path: Path):
    target = tmp_path / "outside" / "loot.txt"
    target.write_text("secret")
    link = sandbox / "innocent.txt"
    os.symlink(target, link)
    assert policy.is_path_allowed(link) is False


def test_risk_tiers():
    assert SecurityPolicy.requires_approval(RiskLevel.HIGH) is True
    assert SecurityPolicy.requires_approval(RiskLevel.LOW) is False
    assert SecurityPolicy.requires_approval(RiskLevel.SAFE) is False
    assert SecurityPolicy.requires_notification(RiskLevel.LOW) is True


# --- Pre-execution validation ----------------------------------------------------------


def test_validation_rejects_bad_schema(policy: SecurityPolicy):
    tool = ListDirectoryTool(policy)
    with pytest.raises(ValidationError, match="directory_path"):
        validate_tool_call(tool, {}, policy)
    with pytest.raises(ValidationError):
        validate_tool_call(tool, {"directory_path": 42}, policy)


def test_validation_rejects_paths_outside_sandbox(sandbox: Path, policy: SecurityPolicy):
    tool = ListDirectoryTool(policy)
    with pytest.raises(ValidationError, match="Access denied"):
        validate_tool_call(tool, {"directory_path": "/etc"}, policy)
    validate_tool_call(tool, {"directory_path": str(sandbox)}, policy)


def test_validation_checks_every_path_in_lists(sandbox: Path, policy: SecurityPolicy):
    tool = BulkMoveFilesTool(policy)
    args = {
        "source_paths": [str(sandbox / "a.txt"), "/etc/passwd"],
        "destination_directory": str(sandbox),
    }
    with pytest.raises(ValidationError, match="/etc/passwd"):
        validate_tool_call(tool, args, policy)
