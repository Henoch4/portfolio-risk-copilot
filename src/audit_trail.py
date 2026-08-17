"""
Immutable, append-only local audit trail.

Every governance decision gets logged -- not just fills: risk-gate rejections,
curator switches, integrity blocks. JSONL keeps it trivially append-only and
greppable for reconciliation. This complements the on-chain audit logger:
the chain proves the decision was signed; this file records the reasoning and
the exact inputs that produced it.

Ported from the sibling `trading_system` MVP (audit/audit_log.py).
"""
from __future__ import annotations

import json
import time
from pathlib import Path


class AuditLog:
    def __init__(self, path: str | Path = "audit_log.jsonl"):
        self.path = Path(path)

    def write(self, event_type: str, payload: dict):
        """Append one record. Append-only: readers must not rewrite the file."""
        record = {
            "ts": time.time(),
            "event_type": event_type,
            "payload": _jsonable(payload),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


def _jsonable(obj):
    """Best-effort conversion of dataclasses/enums nested in payloads.

    Enum must be checked before the generic object branch: Enum members have
    a ``__dict__`` too, and converting that recurses into the member map.
    """
    if hasattr(obj, "value") and hasattr(obj, "name"):  # Enum
        return obj.value
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {k: _jsonable(v) for k, v in vars(obj).items()}
    return obj