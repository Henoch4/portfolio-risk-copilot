"""Minimal Phase-1 alerting: fire-and-forget webhook notifications.

The kill switch must not exist only in logs (roadmap Phase 1): if the gate
halts trading or the durable risk-state file cannot be written, someone has
to hear about it without tailing server output.

Design constraints:
- Never raises, never blocks the trading loop: short timeout, swallow-all
  failure policy. Alerting is best-effort by definition; it must not become
  a new way for the trading loop to die.
- Generic JSON webhook (works with bare receivers and most relay formats);
  ALERT_WEBHOOK_URL unset means disabled with zero overhead.
- Per-event cooldown so a hot loop of persist failures cannot flood the
  channel. KILL_SWITCH_ACTIVATED uses a much shorter cooldown: it is rare,
  state-gated upstream, and must effectively always land.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_COOLDOWN_SECONDS = 300.0
KILL_SWITCH_COOLDOWN_SECONDS = 5.0

_CRITICAL_EVENTS = {"KILL_SWITCH_ACTIVATED", "KILL_SWITCH_DEACTIVATED"}

_lock = threading.Lock()
_last_sent: dict[str, float] = {}


def post_json(url: str, payload: dict, timeout: float) -> None:
    """Transport seam: single POST, raises on any failure."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=timeout).close()


def _cooldown_for(event: str) -> float:
    if event in _CRITICAL_EVENTS:
        return KILL_SWITCH_COOLDOWN_SECONDS
    return float(os.environ.get("ALERT_COOLDOWN_SECONDS", "").strip() or DEFAULT_COOLDOWN_SECONDS)


def send_alert(event: str, severity: str, detail: str) -> bool:
    """Send an alert webhook. Returns True if sent, False if skipped/failed.

    Reads ALERT_WEBHOOK_URL at call time so tests and runtime config changes
    don't require reimport. Cooldown is keyed by event name and process-local.
    """
    url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return False

    now = time.monotonic()
    with _lock:
        last = _last_sent.get(event)
        if last is not None and (now - last) < _cooldown_for(event):
            logger.warning(
                "ALERT_SUPPRESSED(cooldown): %s — %s", event, detail
            )
            return False
        _last_sent[event] = now

    payload = {
        "event": event,
        "severity": severity,
        "detail": detail,
        "timestamp": int(time.time()),
    }
    try:
        post_json(url, payload, DEFAULT_TIMEOUT_SECONDS)
        logger.warning("ALERT_SENT: %s — %s", event, detail)
        return True
    except Exception as e:  # noqa: BLE001 — alerting must never propagate
        logger.error("ALERT_FAILED: %s — %s (%s)", event, detail, e)
        return False


def reset_cooldowns() -> None:
    """Test seam: clear the process-local cooldown table."""
    with _lock:
        _last_sent.clear()
