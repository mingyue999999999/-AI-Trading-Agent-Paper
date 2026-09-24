import base64
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

THRESHOLD_MINUTES = 55.0
POLL_SECONDS = 5
RUN_TIMEOUT_SECONDS = 2400

COMPONENTS = {
    "spot": "spot-paper.yml",
    "futures": "futures-paper.yml",
    "quant": "quant-paper.yml",
    "meme": "meme-paper.yml",
    "polymarket": "polymarket-paper.yml",
    "valuation": "valuation-snapshot.yml",
}

token = os.environ["GITHUB_TOKEN"]
repo = os.environ["GITHUB_REPOSITORY"]

def api_request(url, method="GET", payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None

def parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def workflow_runs(workflow_file):
    wf = urllib.parse.quote(workflow_file, safe="")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs?per_page=30"
    return api_request(url).get("workflow_runs", [])

def valuation_observed_at():
    url = f"https://api.github.com/repos/{repo}/contents/state/valuations/latest.json?ref=main"
    obj = api_request(url)
    raw = base64.b64decode(obj["content"]).decode("utf-8")
    return parse_ts(json.loads(raw).get("observed_at"))

def component_state(component, workflow_file):
    now = datetime.now(timezone.utc)
    runs = workflow_runs(workflow_file)
    active = any(r.get("status") in {"queued", "in_progress", "waiting", "requested", "pending"} for r in runs)

    if component == "valuation":
        freshness = valuation_observed_at()
        source = "valuation_observed_at"
    else:
        freshness = None
        source = "last_successful_run"
        for r in runs:
            if r.get("status") == "completed" and r.get("conclusion") == "success":
                freshness = parse_ts(r.get("updated_at") or r.get("run_started_at") or r.get("created_at"))
                break

    age = float("inf") if freshness is None else max(0.0, (now - freshness).total_seconds() / 60.0)
    return {
        "component": component,
        "workflow": workflow_file,
        "age_minutes": age,
        "active": active,
        "stale": age > THRESHOLD_MINUTES,
        "freshness_source": source,
    }

def all_states():
    return [component_state(component, workflow) for component, workflow in COMPONENTS.items()]

def dispatch_and_wait(component, workflow_file):
    before = {r["id"] for r in workflow_runs(workflow_file)}
    wf = urllib.parse.quote(workflow_file, safe="")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/dispatches"
    started = datetime.now(timezone.utc)
    api_request(url, method="POST", payload={"ref": "main"})
    print(f"dispatched {component} via {workflow_file}")

    deadline = time.time() + RUN_TIMEOUT_SECONDS
    run_id = None

    while time.time() < deadline:
        runs = workflow_runs(workflow_file)
        fresh = [
            r for r in runs
            if r.get("id") not in before
            and r.get("event") == "workflow_dispatch"
            and parse_ts(r.get("created_at")) is not None
            and parse_ts(r.get("created_at")) >= started
        ]
        if fresh:
            fresh.sort(key=lambda r: r.get("created_at", ""), reverse=True)
            run = fresh[0]
            run_id = run["id"]
            if run.get("status") == "completed":
                if run.get("conclusion") != "success":
                    raise SystemExit(f"{component} recovery run {run_id} ended as {run.get('conclusion')}")
                print(f"{component} recovered successfully in run {run_id}")
                return
        time.sleep(POLL_SECONDS)

    raise SystemExit(f"{component} recovery timed out; run_id={run_id}")

def print_states(states):
    compact = []
    for s in states:
        compact.append({
            "component": s["component"],
            "age_minutes": None if s["age_minutes"] == float("inf") else round(s["age_minutes"], 1),
            "active": s["active"],
            "stale": s["stale"],
            "freshness_source": s["freshness_source"],
        })
    print(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(), "components": compact}, ensure_ascii=False))

# Recover strictly one component at a time. Re-evaluate after every successful recovery.
for _ in range(len(COMPONENTS)):
    states = all_states()
    print_states(states)
    candidates = [s for s in states if s["stale"] and not s["active"]]
    if not candidates:
        print("all core PAPER components are fresh or already active")
        break
    selected = max(candidates, key=lambda s: s["age_minutes"])
    dispatch_and_wait(selected["component"], selected["workflow"])
else:
    final_states = all_states()
    remaining = [s for s in final_states if s["stale"] and not s["active"]]
    if remaining:
        raise SystemExit("watchdog exhausted recovery passes while stale components remain")
