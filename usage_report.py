#!/usr/bin/env python3
"""GenAI gateway usage report.

SPEND section        -> this user's cycle spend + lifetime spend
TEAM BUDGETS section -> each team's budget, usage %, and cycle period

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
    if not d:
        return None
    try:
        n, unit = int(str(d)[:-1]), str(d)[-1]
    except (ValueError, IndexError):
        return None
    return {"d": timedelta(days=n), "h": timedelta(hours=n),
            "m": timedelta(minutes=n), "s": timedelta(seconds=n)}.get(unit)


def time_until(reset_raw):
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
    end = to_dt(reset_raw)
    dur = parse_duration(duration)
    if end and dur:
        return end - dur
    return None


# ------------------- per-user spend within a team (cycle) -------------------
def user_team_cycle_spend(token, uid, team_id, since_dt):
    """Sum this user's spend in a team since `since_dt` (None = all time).
    Returns float, or None if logs unavailable/unauthorized."""
    logs = api_get(f"/spend/logs?user_id={uid}", token)
    if isinstance(logs, dict) and "_error" in logs:
        return None
    rows = logs if isinstance(logs, list) else logs.get("data", logs.get("logs", []))
    if not isinstance(rows, list):
        return None
    total = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        if team_id and r.get("team_id") not in (team_id, None):
            continue
        ts = to_dt(r.get("startTime") or r.get("created_at") or r.get("timestamp"))
        if since_dt and ts and ts < since_dt:
            continue
        total += r.get("spend", 0) or 0
    return total


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

    # ---- user info (all team memberships + lifetime spend) ----
    user = {}
    if uid:
        ur = api_get(f"/user/info?user_id={uid}", api_key)
        user = ur if "_error" not in ur else {}
    user_info = user.get("user_info", user)
    lifetime_spend = user_info.get("spend")
    created_at = user_info.get("created_at") or info.get("created_at")

    # ---- enumerate all the user's teams ----
    team_ids = set()
    for src in (user.get("teams") or []), (user.get("user_info", {}).get("teams") or []):
        for t in src:
            tid = t.get("team_id") if isinstance(t, dict) else t
            if tid:
                team_ids.add(tid)
    if key_team_id:
        team_ids.add(key_team_id)
    teams = []
    for tid in sorted(team_ids):
        tr = api_get(f"/team/info?team_id={tid}", api_key)
        obj = tr.get("team_info", tr) if "_error" not in tr else {}
        obj["resolved_team_id"] = tid
        teams.append(obj)

    # ---- this user's cycle spend in the governing team (for SPEND section) ----
    my_cycle_spend = None
    gov_budget = team.get("max_budget")
    if key_team_id:
        since = cycle_start(team.get("budget_reset_at"), team.get("budget_duration"))
        my_cycle_spend = user_team_cycle_spend(api_key, uid, key_team_id, since)

    models = info.get("models") or []

    # ============================ render ============================
    line = "=" * 55
    print(line)
    print("            GenAI Gateway — API Key Usage Report")
    print(line)
    print(f"Key alias      : {info.get('key_alias') or '—'}")
    print(f"User           : {info.get('user_id') or '—'}")
    print(f"Team           : {info.get('team_id') or '—'}")
    print(f"Models allowed : {'all' if not models else ', '.join(models)}")
    print()

    # ---------------- SPEND: this user's numbers ----------------
    print("------------------------ SPEND ------------------------")
    if my_cycle_spend is not None:
        pct = f" ({my_cycle_spend / gov_budget * 100:.1f}% of team budget)" if gov_budget else ""
        print(f"Cycle spend    : {money(my_cycle_spend)}{pct}")
    else:
        # fall back to the key/governing spend if logs unavailable
        fallback = info.get("spend") if info.get("max_budget") is not None else team.get("spend")
        print(f"Cycle spend    : {money(fallback)}")
    if lifetime_spend is not None:
        since_str = f" (since {str(created_at)[:10]})" if created_at else ""
        print(f"Lifetime spend : {money(lifetime_spend)}{since_str}")
    print()

    # ---------------- TEAM BUDGETS: the team's numbers ----------------
    print("--------------------- TEAM BUDGETS --------------------")
    if not teams:
        print("  (no team budgets — usage is key- or user-managed)")
    for t in teams:
        tid = t.get("resolved_team_id")
        name = t.get("team_alias") or tid or "—"
        tbudget = t.get("max_budget")
        tspend = t.get("spend") or 0
        gov = "  ← governs this key" if tid == key_team_id else ""
        print(f"  • {name}{gov}")
        if tbudget is not None:
            tpct = (tspend / tbudget * 100) if tbudget else 0
            print(f"      Budget       : ${tbudget}")
            print(f"      Team usage   : {money2(tspend)} ({tpct:.1f}%)")
        else:
            print(f"      Budget       : unlimited")
            print(f"      Team usage   : {money2(tspend)}")
        cyc = t.get("budget_duration")
        reset = t.get("budget_reset_at")
        if cyc or reset:
            tleft = time_until(reset)
            cyc_str = cyc or "—"
            reset_str = f"{reset}" + (f" (in {tleft})" if tleft else "") if reset else "—"
            print(f"      Cycle period : {cyc_str}")
            print(f"      Resets       : {reset_str}")
    print()
    print(line)


if __name__ == "__main__":
    main()
