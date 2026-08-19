"""Shared contract utilities — ABI loading and web3 helpers."""
from __future__ import annotations

import json
import os
import pathlib


def load_abi(name: str) -> list[dict]:
    """Load a compiled ABI from contracts/artifacts/."""
    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "contracts" / "artifacts" / f"{name}_abi.json"
    )
    if not path.exists():
        return []
    return json.loads(path.read_text())
