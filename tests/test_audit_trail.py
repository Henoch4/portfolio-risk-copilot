"""Unit tests for src/audit_trail.py — the local append-only audit log.

No deps, no network. Run: pytest tests/test_audit_trail.py -v
"""
import json
import re
from enum import Enum
from pathlib import Path

import pytest

from src.audit_trail import AuditLog, _jsonable


class FakeEvt(Enum):
    REJECTED = "rejected"


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit_log.jsonl"


def test_write_creates_append_only_jsonl(audit_path: Path):
    log = AuditLog(audit_path)
    log.write("risk_rejection", {"reason": "STALE_PRICE", "order": {"symbol": "BTC"}})
    log.write("curator_switch", {"profile": "defensive"})

    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        assert json.loads(line)  # every line is valid JSON


def test_record_shape(audit_path: Path):
    log = AuditLog(audit_path)
    log.write("risk_rejection", {"reason": "STALE_PRICE"})
    record = json.loads(audit_path.read_text(encoding="utf-8").strip().splitlines()[0])
    assert set(record) == {"ts", "event_type", "payload"}
    assert record["event_type"] == "risk_rejection"
    assert record["payload"] == {"reason": "STALE_PRICE"}
    assert isinstance(record["ts"], (int, float))


def test_append_only_never_rewrites(audit_path: Path):
    log = AuditLog(audit_path)
    log.write("one", {"n": 1})
    first_lines = audit_path.read_text(encoding="utf-8").strip().splitlines()

    log.write("two", {"n": 2})
    all_lines = audit_path.read_text(encoding="utf-8").strip().splitlines()

    assert all_lines[: len(first_lines)] == first_lines
    assert len(all_lines) == len(first_lines) + 1


def test_jsonable_handles_enum(audit_path: Path):
    log = AuditLog(audit_path)
    log.write("risk_rejection", {"reason": FakeEvt.REJECTED})
    record = json.loads(audit_path.read_text(encoding="utf-8").strip().splitlines()[0])
    assert record["payload"]["reason"] == "rejected"


def test_jsonable_recurses_dataclass(audit_path: Path):
    from dataclasses import dataclass

    @dataclass
    class Order:
        cl_ord_id: str

    log = AuditLog(audit_path)
    log.write("order_submitted", {"order": Order("abc-1")})
    record = json.loads(audit_path.read_text(encoding="utf-8").strip().splitlines()[0])
    assert record["payload"]["order"] == {"cl_ord_id": "abc-1"}


def test_missing_parent_dir_raises_for_failure_isolation(audit_path: Path):
    # Caller must create directories; failing loudly beats silently logging nowhere.
    log = AuditLog(audit_path.parent / "nope" / "audit.jsonl")
    with pytest.raises(FileNotFoundError):
        log.write("risk_rejection", {"reason": "STALE_PRICE"})


def test_iso_timestamp_line_does_not_corrupt_json(audit_path: Path):
    log = AuditLog(audit_path)
    log.write("with_str_value", {"note": "funding rate NaN -> HARD BLOCK"})
    raw = audit_path.read_text(encoding="utf-8")
    assert not re.search(r"\n\s*\n", raw)  # no empty/partial lines
    json.loads(raw.strip().splitlines()[0])