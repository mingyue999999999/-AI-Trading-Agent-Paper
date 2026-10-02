from pathlib import Path

ACTIVE_SCHEDULES = {
    ".github/workflows/spot-paper.yml": [
        "workflow_dispatch:", "schedule:", 'cron: "7,37 * * * *"',
        'timezone: "Etc/UTC"', "group: spot-paper-trading-final",
        ".github/heartbeat/spot.txt", "github.event_name != 'pull_request'",
    ],
    ".github/workflows/futures-paper.yml": [
        "workflow_dispatch:", "schedule:", 'cron: "12,42 * * * *"',
        'timezone: "Etc/UTC"', "group: futures-paper-trading-final",
        ".github/heartbeat/futures.txt", "github.event_name != 'pull_request'",
    ],
}

STOPPED_WORKFLOWS = {
    ".github/workflows/meme-paper.yml",
    ".github/workflows/polymarket-paper.yml",
}

for filename, required in ACTIVE_SCHEDULES.items():
    text = Path(filename).read_text(encoding="utf-8")
    missing = [item for item in required if item not in text]
    if missing:
        raise SystemExit(f"{filename} schedule safety check failed; missing: {missing}")

for filename in STOPPED_WORKFLOWS:
    text = Path(filename).read_text(encoding="utf-8")
    if "workflow_dispatch:" not in text:
        raise SystemExit(f"{filename} stopped-workflow check failed; manual review entry missing")
    forbidden = [item for item in ("schedule:", "\n  push:") if item in text]
    if forbidden:
        raise SystemExit(
            f"{filename} stopped-workflow check failed; unexpected automatic triggers: {forbidden}"
        )

print("active schedules and stopped PAPER workflows configuration check passed")
