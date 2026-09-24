from pathlib import Path

EXPECTED = {
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
    ".github/workflows/meme-paper.yml": [
        "workflow_dispatch:", "schedule:", 'cron: "17,47 * * * *"',
        'timezone: "Etc/UTC"', "group: meme-paper-final",
        ".github/heartbeat/meme.txt", "github.event_name != 'pull_request'",
    ],
}

for filename, required in EXPECTED.items():
    text = Path(filename).read_text(encoding="utf-8")
    missing = [item for item in required if item not in text]
    if missing:
        raise SystemExit(f"{filename} schedule safety check failed; missing: {missing}")

print("all public PAPER schedules configuration check passed")
