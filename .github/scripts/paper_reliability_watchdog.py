import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

THRESHOLD_MINUTES = 55.0
POLL_SECONDS = 5
RUN_DISCOVERY_TIMEOUT_SECONDS = 180

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
    retryable_statuses = {429, 502, 503, 504}

    for attempt in range(4):
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
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code not in retryable_statuses or attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise

        time.sleep(2 ** attempt)


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


def recovery_decision(states):
    """Return at most one recovery decision, preserving global writer serialization."""
    if any(state["active"] for state in states):
        return "active", None

    candidates = [state for state in states if state["stale"]]
    if not candidates:
        return "fresh", None

    return "recover", max(candidates, key=lambda state: state["age_minutes"])


def dispatch_and_wait(component, workflow_file):
    """Dispatch one recovery and only wait until GitHub acknowledges a run.

    The target workflow owns its own concurrency and timeout. Keeping the
    watchdog runner alive until target completion wastes Actions minutes and
    can race with overlapping scheduled runs. A later watchdog invocation
    verifies completion and freshness.
    """
    before = {r["id"] for r in workflow_runs(workflow_file)}
    wf = urllib.parse.quote(workflow_file, safe="")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/dispatches"
    started = datetime.now(timezone.utc)
    api_request(url, method="POST", payload={"ref": "main"})
    print(f"dispatched {component} via {workflow_file}", flush=True)

    deadline = time.time() + RUN_DISCOVERY_TIMEOUT_SECONDS
    overlap_floor = started - timedelta(seconds=5)

    while time.time() < deadline:
        runs = workflow_runs(workflow_file)
        new_runs = [r for r in runs if r.get("id") not in before]
        if new_runs:
            new_runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
            run = new_runs[0]
            run_id = run["id"]
            if run.get("status") == "completed" and run.get("conclusion") != "success":
                raise SystemExit(f"{component} recovery run {run_id} ended as {run.get('conclusion')}")
            if run.get("status") == "completed":
                print(f"{component} recovered successfully in run {run_id}", flush=True)
            else:
                print(
                    f"{component} recovery accepted in run {run_id} "
                    f"({run.get('status')}); completion will be verified later",
                    flush=True,
                )
            return

        # A scheduled or separately dispatched run can overlap the request.
        # Treat a recent success as recovery instead of waiting for a second ID.
        recent_success = [
            r for r in runs
            if r.get("status") == "completed"
            and r.get("conclusion") == "success"
            and parse_ts(r.get("updated_at") or r.get("run_started_at") or r.get("created_at")) is not None
            and parse_ts(r.get("updated_at") or r.get("run_started_at") or r.get("created_at")) >= overlap_floor
        ]
        if recent_success:
            recent_success.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
            print(
                f"{component} recovered by overlapping run {recent_success[0]['id']}",
                flush=True,
            )
            return
        time.sleep(POLL_SECONDS)

    raise SystemExit(f"{component} recovery dispatch was not observable within {RUN_DISCOVERY_TIMEOUT_SECONDS}s")


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


def main():
    states = all_states()
    print_states(states)
    decision, selected = recovery_decision(states)

    if decision == "active":
        print("a core PAPER workflow is already active; deferring recovery to avoid concurrent writers")
        return
    if decision == "fresh":
        print("all core PAPER components are fresh")
        return

    dispatch_and_wait(selected["component"], selected["workflow"])
    final_states = all_states()
    print_states(final_states)
    remaining = [state for state in final_states if state["stale"] and not state["active"]]
    if remaining:
        print("additional stale components deferred to the next watchdog check")


if __name__ == "__main__":
    main()
