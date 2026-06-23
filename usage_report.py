#!/usr/bin/env python3
"""GenAI gateway usage report — key/team budgets + per-user spend this cycle.

Usage:
    python3 usage_report.py sk-your-api-key
    API_KEY=sk-your-api-key python3 usage_report.py
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

GATEWAY = os.environ.get("GATEWAY", "https://gateway.aws.genai.umbc.edu")


# ----------------------------- HTTP helper -----------------------------
def api_get(path, token):
    """GET a gateway endpoint; return parsed JSON, or {'_error': ...} on failure."""
    req = urllib.request.Request(f"{GATEWAY}{path}",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except urllib.error.URLError as e:
        return {"_error": f"Connection error: {e.reason}"}
    except json.JSONDecodeError:
        return {"_error": "non-JSON response"}


# ----------------------------- formatting -----------------------------
def money(v):
    return f"${(v or 0):.4f}"


def money2(v):
    return f"${(v or 0):.2f}"


def to_dt(s):
    """Parse an epoch number or ISO string into an aware UTC datetime, or None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    s = str(s).replace("Z", "").split(".")[0]
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        return None


def parse_duration(d):
    """'7d'/'24h'/'30m' -> timedelta, or None."""
    if not d:
        return None
    try:
        n, unit = int(str(d)[:-1]), str(d)[-1]
    except (ValueError, IndexError):
        return None
    return {"d": timedelta(days=n), "h": timedelta(hours=n),
            "m": timedelta(minutes=n), "s": timedelta(seconds=n)}.get(unit)


def time_until(reset_raw):
    """Return 'Xd Yh' / 'Yh Zm' / 'Zm' until reset, or None."""
    target = to_dt(reset_raw)
    if target is None:
        return None
    secs = max(0, int(target.timestamp() - time.time()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def cycle_start(reset_raw, duration):
    """Start of the current budget cycle = reset_at - duration."""
    end = to_dt(reset_raw)
    dur = parse_duration(duration)
    if end and dur:
        return end - dur
    return None


# ------------------- per-user spend within a team (cycle) -------------------
def user_team_cycle_spend(token, uid, team_id, since_dt):
    """Sum this user's spend in a team since `since_dt`, from /spend/logs.

    Returns a float, or None if logs are unavailable / unauthorized.
    """
    logs = api_get(f"/spend/logs?user_id={uid}", token)
    if isinstance(logs, dict) and "_error" in logs:
        return None
    rows = logs if isinstance(logs, list) else logs.get("data", logs.get("logs", []))
    if not isinstance(rows, list):
        return None
    total = 0.0
    found = False
    for r in rows:
        if not isinstance(r, dict):
            continue
        if team_id and r.get("team_id") not in (team_id, None):
            continue
        ts = to_dt(r.get("startTime") or r.get("created_at") or r.get("timestamp"))
        if since_dt and ts and ts < since_dt:
            continue
        total += r.get("spend", 0) or 0
        found = True
    return total if found else 0.0


# ------------------------------- main -------------------------------
def main():
    api_key = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("API_KEY")
    if not api_key:
        sys.exit("Usage: usage_report.py sk-your-api-key   (or set API_KEY env var)")

    # ---- key info ----
    key_resp = api_get(f"/key/info?key={api_key}", api_key)
    if "_error" in key_resp:
        sys.exit(f"Error fetching key info: {key_resp['_error']}")
    if "error" in key_resp or "detail" in key_resp:
        sys.exit(f"Gateway error: {key_resp.get('error') or key_resp.get('detail')}")

    info = key_resp.get("info", {})
    uid = info.get("user_id")
    key_team_id = info.get("team_id")

    # ---- governing team ----
    team = {}
    if key_team_id:
        tr = api_get(f"/team/info?team_id={key_team_id}", api_key)
        team = tr.get("team_info", tr) if "_error" not in tr else {}

    # ---- user info (all team memberships) ----
    user = {}
    if uid:
        ur = api_get(f"/user/info?user_id={uid}", api_key)
        user = ur if "_error" not in ur else {}

    # ---- enumerate all the user's teams ----
    team_ids = set()
    for src in (user.get("teams") or []), (user.get("user_info", {}).get("teams") or []):
        for t in src:
            tid = t.get("team_id") if isinstance(t, dict) else t
            if tid:
                team_ids.add(tid)
    teams = []
    for tid in sorted(team_ids):
        tr = api_get(f"/team/info?team_id={tid}", api_key)
        obj = tr.get("team_info", tr) if "_error" not in tr else {}
        obj["resolved_team_id"] = tid
        teams.append(obj)

    # ---- resolve governing budget: key first, else its team ----
    if info.get("max_budget") is not None:
        budget, spend, src = info["max_budget"], info.get("spend") or 0, "key"
        reset_raw, cycle = info.get("budget_reset_at"), info.get("budget_duration")
    elif team.get("max_budget") is not None:
        budget, spend, src = team["max_budget"], team.get("spend") or 0, "team"
        reset_raw, cycle = team.get("budget_reset_at"), team.get("budget_duration")
    else:
        budget, spend, src = None, info.get("spend") or 0, None
        reset_raw, cycle = None, None

    tleft = time_until(reset_raw)
    models = info.get("models") or []

    # ---- this user's spend in the governing team, this cycle ----
    my_share = None
    if src == "team":
        since = cycle_start(reset_raw, cycle)
        my_share = user_team_cycle_spend(api_key, uid, key_team_id, since)

    # ----------------------------- render -----------------------------
    line = "=" * 55
    print(line)
    print("            GenAI Gateway — API Key Usage Report")
    print(line)
    print(f"Key alias      : {info.get('key_alias') or '—'}")
    print(f"User / Owner   : {info.get('user_id') or '—'}")
    print(f"Team           : {info.get('team_id') or '—'}")
    print()
    print("---------------------- SPEND --------------------------")
    print(f"Spent          : {money(spend)}")
    if budget is None:
        print("Budget (max)   : unlimited")
        print("Remaining      : unlimited")
        print("Usage          : —")
    else:
        tag = " (team)" if src == "team" else ""
        print(f"Budget (max)   : ${budget}{tag}")
        print(f"Remaining      : {money(budget - spend)}")
        print(f"Usage          : {(spend / budget * 100) if budget else 0:.1f}%")
    if cycle:
        print(f"Budget cycle   : {cycle}")
    if reset_raw:
        suffix = f" (in {tleft})" if tleft else ""
        print(f"Budget resets  : {reset_raw}{suffix}")
    if src == "team" and my_share is not None:
        share_pct = f" ({my_share / budget * 100:.1f}% of team budget)" if budget else ""
        print(f"Your spend     : {money(my_share)}{share_pct}  [this cycle]")
    print(f"Models allowed : {'all' if not models else ', '.join(models)}")

    # ---- per-team breakdown for multi-team users ----
    if len(teams) > 1:
        print()
        print("------------------ TEAM BUDGETS -----------------------")
        for t in teams:
            tid = t.get("resolved_team_id")
            name = t.get("team_alias") or tid or "—"
            if t.get("max_budget") is not None:
                budget_str = f"team {money2(t.get('spend'))}/${t['max_budget']}"
            else:
                budget_str = f"team {money2(t.get('spend'))} (no budget)"
            since = cycle_start(t.get("budget_reset_at"), t.get("budget_duration"))
            mine = user_team_cycle_spend(api_key, uid, tid, since)
            you = f", you {money2(mine)} this cycle" if mine is not None else ""
            gov = "   ← governs this key" if tid == key_team_id else ""
            print(f"  • {name}: {budget_str}{you}{gov}")
    print()
    print(line)


if __name__ == "__main__":
    main()