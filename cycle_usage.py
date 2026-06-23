#!/usr/bin/env python3
"""User's spend within a team during the current budget cycle."""
import json, os, sys, time
import urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

GATEWAY = os.environ.get("GATEWAY", "https://gateway.aws.genai.umbc.edu")

def api_get(path, token):
    req = urllib.request.Request(f"{GATEWAY}{path}",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def parse_dur(d):
    """'7d' -> timedelta. Supports d/h/m."""
    if not d: return None
    n, unit = int(d[:-1]), d[-1]
    return {"d": timedelta(days=n), "h": timedelta(hours=n),
            "m": timedelta(minutes=n)}.get(unit)

def to_dt(s):
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s, tz=timezone.utc)
    s = str(s).replace("Z", "").split(".")[0]
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

def main():
    key = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("API_KEY")
    team_id = sys.argv[2] if len(sys.argv) > 2 else None
    if not key:
        sys.exit("Usage: cycle_usage.py sk-KEY [team_id]")

    info = api_get(f"/key/info?key={key}", key).get("info", {})
    uid = info.get("user_id")
    team_id = team_id or info.get("team_id")

    team = api_get(f"/team/info?team_id={team_id}", key)
    t = team.get("team_info", team)
    reset_at = t.get("budget_reset_at") or info.get("budget_reset_at")
    duration = t.get("budget_duration") or info.get("budget_duration")

    # cycle window: [reset_at - duration, now]
    now = datetime.now(timezone.utc)
    if reset_at and duration:
        cycle_start = to_dt(reset_at) - parse_dur(duration)
    else:
        cycle_start = now - timedelta(days=30)  # fallback

    # pull this user's logs and sum within team + window
    logs = api_get(f"/spend/logs?user_id={uid}", key)
    rows = logs if isinstance(logs, list) else logs.get("data", logs.get("logs", []))

    total = 0.0
    for r in rows:
        if r.get("team_id") != team_id:
            continue
        ts = r.get("startTime") or r.get("created_at") or r.get("timestamp")
        if ts and to_dt(ts) < cycle_start:
            continue
        total += r.get("spend", 0) or 0

    print(f"User           : {uid}")
    print(f"Team           : {team_id}")
    print(f"Cycle start    : {cycle_start.isoformat()}")
    print(f"Cycle resets   : {reset_at}")
    print(f"Your spend     : ${total:.4f}  (this cycle, this team)")
    if t.get("max_budget"):
        print(f"Team budget    : ${t['max_budget']}")
        print(f"You used       : {total / t['max_budget'] * 100:.1f}% of team budget")

if __name__ == "__main__":
    main()