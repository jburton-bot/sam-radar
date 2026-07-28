#!/usr/bin/env python3
"""Run the SAM Radar morning routine once (sync SAM.gov, then email the digest).

Built for headless runners like GitHub Actions: no web server, no browser.
Secrets arrive through environment variables (see .github/workflows/daily.yml):

  SAM_API_KEY            SAM.gov API key
  RADAR_SMTP_USERNAME    Gmail address that sends the digest
  RADAR_SMTP_PASSWORD    Gmail App Password (16 characters, no spaces)
  RADAR_TO_ADDRESSES     Comma-separated recipient list
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def ensure_email_enabled() -> None:
    """The dashboard normally flips email on; a headless run must do it here.

    Only the non-secret ``enabled`` flag is written. Secrets stay in
    environment variables and are never saved to disk.
    """
    settings_path = Path(os.getenv("RADAR_SETTINGS_PATH", HERE / "settings.json"))
    data = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    data.setdefault("email", {})["enabled"] = True
    settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> int:
    ensure_email_enabled()
    import server
    server.init_db()
    outcome = server.morning_routine()
    server.record_auto_result(outcome)
    print(outcome)
    failed = (
        outcome == "No API key"
        or outcome.startswith("Sync stopped")
        or "Email not sent" in outcome
        or "A sync is already running" in outcome
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
