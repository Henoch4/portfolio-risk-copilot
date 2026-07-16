"""
Thin subprocess wrapper around the `okx` CLI (@okx_ai/okx-trade-cli).

This is the ONLY module in the project that shells out to `okx`. Every
command and flag below was verified against the ingested agent-trade-kit
repo (docs/cli-reference.md, skills/okx-cex-portfolio/SKILL.md,
skills/okx-cex-smartmoney/SKILL.md) -- nothing here is invented.

Two things this file does NOT do, on purpose:
  1. It never accepts or stores OKX credentials. Auth lives entirely in
     ~/.okx/config.toml (API-key mode) or the OAuth session created by
     `okx auth login` (OAuth mode) -- both managed by the CLI itself.
  2. It never calls a write command (place/cancel/transfer/etc). Only
     read commands are wired up, matching the ASP's read-only manifest.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass


class OkxCliError(RuntimeError):
    """Raised when the `okx` CLI exits non-zero (exit code 1 per docs) or
    when the binary itself isn't on PATH."""

    def __init__(self, args: list, returncode: int, stderr: str):
        self.args = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"okx {' '.join(args)} failed (exit {returncode}): {stderr.strip()}"
        )


@dataclass
class OkxCliConfig:
    demo: bool = True                 # default: demo/simulated trading
    profile: str | None = None        # API-key profile name, if using API-key auth


def _binary_available() -> bool:
    return shutil.which("okx") is not None


class OkxCli:
    """Async wrapper. Create one instance per audit request."""

    def __init__(self, config: OkxCliConfig):
        self.config = config

    def _global_flags(self) -> list:
        # Verified in skills/okx-cex-portfolio/SKILL.md "Demo vs Live Mode":
        #   API-key user: --profile <name>   (selects a demo or live profile)
        #   OAuth user:   --demo             (live is the default with no flag)
        # These are mutually exclusive.
        if self.config.profile:
            return ["--profile", self.config.profile]
        if self.config.demo:
            return ["--demo"]
        return []

    async def run(self, *args: str, use_global_flags: bool = True):
        """Run `okx <args> --json` and parse the JSON result.

        use_global_flags=False is used for `config show` / `auth status` /
        `smartmoney *` calls: none of the documented examples for those
        commands show --demo or --profile, so we don't inject them.
        This is a documented-behavior inference, not a directly-cited
        example -- flag for live verification during testing.
        """
        if not _binary_available():
            raise OkxCliError(
                list(args), -1,
                "`okx` binary not found on PATH. Run "
                "`npm install -g @okx_ai/okx-trade-cli` first (see README.md).",
            )

        flags = self._global_flags() if use_global_flags else []
        cmd = ["okx", *flags, *args, "--json"]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
        except OSError as e:
            # BUGFIX: shutil.which() finding the binary doesn't guarantee
            # exec succeeds (TOCTOU: permissions change, binary removed
            # between check and exec, etc). Without this, a raw OSError
            # would propagate uncaught past check_auth()'s `except
            # OkxCliError` and crash the whole request with a leaked 500.
            raise OkxCliError(list(args), -1, f"failed to launch `okx`: {e}")

        if proc.returncode != 0:
            raise OkxCliError(list(args), proc.returncode, err.decode())

        text = out.decode().strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            # BUGFIX: same leak risk if the CLI ever prints a non-JSON
            # banner/warning to stdout before the JSON payload. Wrap it so
            # every caller only ever has to catch OkxCliError.
            raise OkxCliError(
                list(args), proc.returncode,
                f"CLI returned non-JSON output: {e}. Raw output: {text[:200]!r}",
            )

    # ------------------------------------------------------------------
    # Auth check -- follows the two-source decision table documented in
    # skills/okx-cex-portfolio/SKILL.md "Credential & Profile Check":
    #   1. `okx config show --json` is authoritative for API-key presence.
    #   2. `okx auth status --json` is authoritative for OAuth session state
    #      (its own `apiKey` field is documented as always false, so it
    #      cannot be used alone).
    # ------------------------------------------------------------------
    async def check_auth(self) -> dict:
        """Never raises -- an unauthenticated account is an expected
        outcome the caller must handle, not a crash."""
        config_show = None
        try:
            config_show = await self.run("config", "show", use_global_flags=False)
        except OkxCliError:
            pass

        if isinstance(config_show, dict):
            profiles = config_show.get("profiles", config_show)
            has_api_key = False
            if isinstance(profiles, dict):
                has_api_key = any(
                    isinstance(p, dict) and p.get("api_key") for p in profiles.values()
                )
            elif isinstance(profiles, list):
                has_api_key = any(
                    isinstance(p, dict) and p.get("api_key") for p in profiles
                )
            if has_api_key:
                return {"authenticated": True, "method": "api_key", "detail": config_show}

        try:
            auth_status = await self.run("auth", "status", use_global_flags=False)
        except OkxCliError as e:
            return {"authenticated": False, "method": None, "detail": str(e)}

        status = (auth_status or {}).get("status")
        if status == "logged_in":
            return {"authenticated": True, "method": "oauth", "detail": auth_status}
        if status == "pending":
            return {"authenticated": False, "method": "oauth_pending", "detail": auth_status}
        return {"authenticated": False, "method": None, "detail": auth_status}

    # ------------------------------------------------------------------
    # Verified read commands used by the auditor. Field names in the
    # docstrings are the exact ones documented in SKILL.md.
    # ------------------------------------------------------------------
    async def balance_all(self) -> dict:
        """`okx account balance-all --json`
        Returns {trading: {totalEq, adjEq, details[]}, funding: {details[]},
                 valuation: {...}, meta: {...}}
        """
        return await self.run("account", "balance-all")

    async def positions(self, inst_type: str | None = None):
        """`okx account positions [--instType <type>] --json`
        Returns list of {instId, instType, side, pos, avgPx, upl, lever}
        """
        args = ["account", "positions"]
        if inst_type:
            args += ["--instType", inst_type]
        return await self.run(*args)

    async def smartmoney_signal(self, inst_ccy: str) -> dict:
        """`okx smartmoney signal-overview-by-filter --instCcyList <ccy> --json`
        Returns data including weightedLongRatio, weightedShortRatio,
        netNotionalUsdt. Public/read-only -- no auth mode flags applied.
        """
        return await self.run(
            "smartmoney", "signal-overview-by-filter",
            "--instCcyList", inst_ccy,
            use_global_flags=False,
        )
