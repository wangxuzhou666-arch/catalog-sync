"""The audit log records what happened, never what was seen."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalog_sync.audit import AuditEvent, AuditLog, audited, hash_token_id
from catalog_sync.errors import AuthExpired, RecordFormatError

STAMP = datetime(2026, 10, 26, 8, 30, tzinfo=UTC)


def make_log(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "ws" / "audit.jsonl", clock=lambda: STAMP)


def test_an_event_is_one_json_line_with_the_documented_fields(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append(
        AuditEvent(
            "pull",
            "ok",
            source="mock.materials",
            run_id="r1",
            counts={"items": 5, "failed_items": 0},
            compliance_sha256="ab" * 32,
            credential_hash="cd" * 8,
            sinks=("local",),
        )
    )
    (line,) = log.path.read_text(encoding="utf-8").splitlines()
    assert json.loads(line) == {
        "ts": "2026-10-26T08:30:00+00:00",
        "command": "pull",
        "outcome": "ok",
        "source": "mock.materials",
        "run_id": "r1",
        "profile": None,
        "counts": {"failed_items": 0, "items": 5},
        "compliance_sha256": "ab" * 32,
        "credential_hash": "cd" * 8,
        "sinks": ["local"],
    }


def test_the_log_is_append_only_private_and_ascii(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    log.append(AuditEvent("login", "ok", source="café"))
    first = log.path.read_bytes()
    log.append(AuditEvent("pull", "ok"))
    assert log.path.read_bytes().startswith(first), "earlier lines are never rewritten"
    assert log.path.stat().st_mode & 0o077 == 0
    assert log.path.read_bytes().isascii()
    assert [entry["command"] for entry in log.read()] == ["login", "pull"]
    assert log.read()[0]["source"] == "café"


def test_counts_must_be_numbers_so_content_cannot_ride_in_them() -> None:
    with pytest.raises(TypeError, match="integers"):
        AuditEvent("pull", "ok", counts={"items": "secret"})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="integers"):
        AuditEvent("pull", "ok", counts={"items": True})


def test_reading_a_missing_log_gives_nothing_and_a_damaged_one_is_reported(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    assert log.read() == []
    log.path.parent.mkdir(parents=True)
    log.path.write_text('{"ok": 1}\nnot json\n', encoding="utf-8")
    with pytest.raises(RecordFormatError, match="line 2"):
        log.read()
    log.path.write_text("[1]\n", encoding="utf-8")
    with pytest.raises(RecordFormatError, match="not a JSON object"):
        log.read()


def test_the_token_fingerprint_is_short_stable_and_not_the_token() -> None:
    fingerprint = hash_token_id("jti-123")
    assert fingerprint == hash_token_id("jti-123")
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint or "")
    assert "jti-123" not in (fingerprint or "")
    assert hash_token_id("jti-124") != fingerprint
    assert hash_token_id(None) is None


# --- audited() ----------------------------------------------------------------------------------


def test_a_command_that_succeeds_is_logged_with_what_it_filled_in(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    with audited(log, "normalize", source="s") as scope:
        scope.run_id = "r9"
        scope.counts = {"records": 3}
        scope.sinks.append("local")
    (entry,) = log.read()
    assert (entry["command"], entry["outcome"], entry["run_id"]) == ("normalize", "ok", "r9")
    assert entry["counts"] == {"records": 3} and entry["sinks"] == ["local"]


def test_a_failure_is_logged_by_class_name_only_and_reraised(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    with pytest.raises(AuthExpired), audited(log, "pull", source="s"):
        raise AuthExpired("token Bearer-SECRET-123 was rejected")
    (entry,) = log.read()
    assert entry["outcome"] == "error:AuthExpired"
    assert "SECRET" not in log.path.read_text(encoding="utf-8"), "messages can carry data"


def test_a_kill_is_logged_as_interrupted(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    with pytest.raises(KeyboardInterrupt), audited(log, "pull"):
        raise KeyboardInterrupt
    assert log.read()[0]["outcome"] == "interrupted"


def test_a_command_may_set_its_own_outcome(tmp_path: Path) -> None:
    log = make_log(tmp_path)
    with audited(log, "load") as scope:
        scope.outcome = "dry_run"
    assert log.read()[0]["outcome"] == "dry_run"


def test_an_unwritable_log_fails_a_successful_command_but_never_hides_a_real_error(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the workspace directory should be", encoding="utf-8")
    log = AuditLog(blocked / "audit.jsonl", clock=lambda: STAMP)
    with pytest.raises(OSError), audited(log, "pull"):
        pass
    with pytest.raises(AuthExpired), audited(log, "pull"):
        raise AuthExpired("the real problem")
