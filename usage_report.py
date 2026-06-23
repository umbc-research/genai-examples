#!/usr/bin/env python3
"""GenAI gateway usage report.

SPEND section        -> this user's cycle spend (% of team budget) + lifetime spend
TEAM BUDGETS section -> each team's budget, usage %, and cycle period

Cycle spend and lifetime spend are both derived from a SINGLE /spend/logs fetch,
so lifetime (all teams, all time) is always >= cycle (governing team, this cycle).
Falls back to the cached /user/info counter if the logs endpoint is unavailable
or times out.

Usage:
    python3 usage_report.py sk-your-api-key
    API_KEY=sk-your-api-key python3 usage_report.py
"""
import json
import os
import socket
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

GATEWAY = os.environ.get("GATEWAY", "https://gateway.aws.genai.umbc.edu")


# ----------------------------- HTTP helper -----------------------------
def api_get(path, token, timeout=15):
    req = urllib.request.Request(f"{GATEWAY}{path}",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return {"_error": f"timeout/connection error: {e}"}
    except json.JSONDecodeError:
        return {"_error": "non-JSON response"}


# ----------------------------- formatting helpers -----------------------------
def money(v):
    return f"${(v or 0):.4f}"


def money2(v):
    return f"${(v or 0):.2f}"


def to_dt(s):
    """Parse epoch number or ISO string -> aware UTC datetime, or None."""
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
    """'Xd Yh' / 'Yh Zm' / 'Zm' until reset, or None."""
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
    """Start of current budget cycle = reset_at - duration, or None."""
    end = to_dt(reset_raw)
    dur = parse_duration(duration)
    if end and dur:
        return end - dur
    return None


# ----------------------------- spend from logs -----------------------------
def fetch_user_logs(token, uid):
    """Fetch this user's spend-log rows (single request). Returns (rows, ok).
       ok=False if the endpoint is unavailable/denied/times out."""
    resp = api_get(f"/spend/logs?user_id={uid}", token)
    if isinstance(resp, dict) and "_error" in resp:
        return [], False
    rows = resp if isinstance(resp, list) else resp.get("data", resp.get("logs", []))
    if not isinstance(rows, list):
        return [], False
    return rows, True


def sum_logs(rows, team_id=None, since_dt=None):
    """Sum spend in rows, optionally filtered by team_id and/or since_dt."""
    total = 0.0
    for r in rows:
        if not isinstance(r, dict):
            continue
        if team_id is not None and r.get("team_id") != team_id:
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
    gov_budget = team.get("max_budget")

    # ---- user info (team memberships + cached lifetime fallback) ----
    user = {}
    if uid:
        ur = api_get(f"/user/info?user_id={uid}", api_key)
        user = ur if "_error" not in ur else {}
    user_info = user.get("user_info", user)
    cached_lifetime = user_info.get("spend")
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

    # ---- logs: ONE fetch, derive BOTH (lifetime >= cycle guaranteed) ----
    if uid:
        log_rows, logs_ok = fetch_user_logs(api_key, uid)
    else:
        log_rows, logs_ok = [], False

    if logs_ok:
        since = cycle_start(team.get("budget_reset_at"), team.get("budget_duration")) if key_team_id else None
        my_cycle_spend = sum_logs(log_rows, team_id=key_team_id, since_dt=since)  # this team, this cycle
        lifetime_spend = sum_logs(log_rows, team_id=None, since_dt=None)          # all teams, all time
    else:
        my_cycle_spend = None
        lifetime_spend = cached_lifetime   # fallback if logs unavailable/timeout

    models = info.get("models") or []

    # ============================ render ============================
    line = "=" * 55
    print(line)
    print("            GenAI Gateway — API Key Usage Report")
    print(line)
    print(f"Key alias      : {info.get('key_alias') or '—'}")
    print(f"User / Owner   : {info.get('user_id') or '—'}")
    print(f"Team           : {info.get('team_id') or '—'}")
    print(f"Models allowed : {'all' if not models else ', '.join(models)}")
    print()

    # ---------------- SPEND: this user's personal numbers ----------------
    print("------------------------ SPEND ------------------------")
    if my_cycle_spend is not None:
        pct = f" ({my_cycle_spend / gov_budget * 100:.1f}% of team budget)" if gov_budget else ""
        print(f"Cycle spend    : {money(my_cycle_spend)}{pct}")
    else:
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
            print(f"      Cycle period : {cyc or '—'}")
            print(f"      Resets       : " + (f"{reset}" + (f" (in {tleft})" if tleft else "") if reset else "—"))
    print()
    print(line)


if __name__ == "__main__":
    main()