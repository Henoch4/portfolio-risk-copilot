"""
FastAPI ASP surface for the Portfolio Risk Copilot.
Exposes /hire (run an audit), /manifest, /health.

Run locally (after completing README.md setup):
    uvicorn src.main:app --reload --port 8000
"""
from __future__ import annotations

import json
import os
import pathlib
from dataclasses import asdict
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .auditor import run_audit
from .okx_cli import OkxCliError

# Safety guard: live-account audits are opt-in at the PROCESS level, not
# just per-request. This means a malformed or malicious request body can
# never silently flip a demo deployment into reading a live account --
# the operator has to explicitly set ALLOW_LIVE=true in the environment.
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

# BUGFIX: manifest.json (the file) and this route used to be two separate,
# hand-maintained copies -- they had already drifted (missing
# input_schema/output_schema/sla_ms here, inconsistent hire_endpoint
# format). Load the file once at startup instead of duplicating it.
_MANIFEST_PATH = pathlib.Path(__file__).resolve().parent.parent / "manifest.json"

app = FastAPI(title="Portfolio Risk Copilot", version="0.1.0")


class HireRequest(BaseModel):
    mode: Literal["own_account"] = Field(
        "own_account", description="Only 'own_account' is supported."
    )
    profile_mode: Literal["demo", "live"] = Field("demo", description="'demo' or 'live'.")
    profile: str | None = Field(
        None, description="API-key profile name from ~/.okx/config.toml, if using API-key auth."
    )
    inst_type: Literal["SWAP", "FUTURES", "OPTION"] | None = Field(
        None, description="Filter positions by type."
    )


@app.get("/manifest")
def manifest():
    if not _MANIFEST_PATH.exists():
        raise HTTPException(500, "manifest.json missing from deployment")
    data = json.loads(_MANIFEST_PATH.read_text())
    data["live_mode_enabled"] = ALLOW_LIVE  # the one field that's genuinely runtime state
    return data


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/hire")
async def hire(req: HireRequest):
    if req.profile_mode == "live" and not ALLOW_LIVE:
        raise HTTPException(
            403,
            "Live-account audits are disabled on this deployment. "
            "Set ALLOW_LIVE=true in the environment to enable (see SETUP.md).",
        )

    # BUGFIX: run_audit()'s internal try/excepts only cover OkxCliError.
    # A malformed-JSON or subprocess-launch failure now also raises
    # OkxCliError (see okx_cli.py fixes), but this catch-all is the last
    # line of defense so /hire never leaks a raw traceback to a caller.
    try:
        report = await run_audit(
            demo=(req.profile_mode == "demo"),
            profile=req.profile,
            inst_type=req.inst_type,
        )
    except OkxCliError as e:
        raise HTTPException(502, f"OKX CLI call failed: {e}")

    return asdict(report)
