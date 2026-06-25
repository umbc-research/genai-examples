#!/usr/bin/env python3
"""GenAI gateway usage report.
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
    if not d:
        return None
    # bare integer/float -> treat as days
    if isinstance(d, (int, float)):
        return timedelta(days=int(d))
    s = str(d)
    # numeric string with no unit -> treat as days
    if s.isdigit():
        return timedelta(days=int(s))
    try:
        n, unit = int(s[:-1]), s[-1]
    except (ValueError, IndexError):
        return None
    return {"d": timedelta(days=n), "h": timedelta(hours=n),
            "m": timedelta(minutes=n), "s": timedelta(seconds=n)}.get(unit)


def fmt_duration(d):
    """Normalize a duration value to a display string."""
    if d is None:
        return None
    if isinstance(d, (int, float)):
        return f"{int(d)}d"
    s = str(d)
    if s.isdigit():
        return f"{s}d"
    return s


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


def fmt_reset(reset, cycle):
    """Build a 'Cycle period' + 'Resets' display tuple."""
    tleft = time_until(reset)
    cyc_str = fmt_duration(cycle) or "none (no reset)"

    if reset:
        reset_str = f"{reset}" + (f" (in {tleft})" if tleft else "")
    else:
        reset_str = "—"

    return cyc_str, reset_str


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


# ----------------------------- member budget lookup -----------------------------
def find_member_budget(team, uid):
    def member_budget_from_team(team):
        bt = team.get("team_member_budget_table") or {}
        if isinstance(bt, dict) and bt:
            return (
                bt.get("max_budget"),
                bt.get("budget_duration"),
                bt.get("budget_reset_at"),
           )
    return (None, None, None)
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

    # ---- key-level budget fields (typically all null; real limit is per-member) ----
    key_budget = info.get("max_budget")
    key_spend = info.get("spend") or 0
    key_cycle = info.get("budget_duration")
    key_reset = info.get("budget_reset_at")

    # ---- governing team ----
    team = {}
    if key_team_id:
        tr = api_get(f"/team/info?team_id={key_team_id}", api_key)
        team = tr.get("team_info", tr) if "_error" not in tr else {}
    gov_budget = team.get("max_budget")
    has_cycle = bool(team.get("budget_duration") or team.get("budget_reset_at"))

    # ---- team-member budget ----
    member_bt = team.get("team_member_budget_table") or {}
    if isinstance(member_bt, dict) and member_bt.get("max_budget") is not None:
        key_budget = member_bt.get("max_budget")
        key_cycle = member_bt.get("budget_duration")
        key_reset = member_bt.get("budget_reset_at")
    else:
        if key_budget is None:
            key_budget = team.get("max_budget")
        if key_cycle is None:
            key_cycle = team.get("budget_duration")
        if key_reset is None:
            key_reset = team.get("budget_reset_at")

    # ---- user info (team memberships) ----
    user = {}
    if uid:
        ur = api_get(f"/user/info?user_id={uid}", api_key)
        user = ur if "_error" not in ur else {}
    user_info = user.get("user_info", user)
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

    # ---- logs: ONE fetch, derive BOTH personal figures (lifetime >= per-team) ----
    if uid:
        log_rows, logs_ok = fetch_user_logs(api_key, uid)
    else:
        log_rows, logs_ok = [], False

    if logs_ok:
        # If the team has no cycle, since=None -> this becomes an all-time team sum.
        since = cycle_start(team.get("budget_reset_at"), team.get("budget_duration")) if key_team_id else None
        my_team_spend = sum_logs(log_rows, team_id=key_team_id, since_dt=since)
        lifetime_spend = sum_logs(log_rows, team_id=None, since_dt=None)
    else:
        my_team_spend = None
        lifetime_spend = None

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

    # ---------------- SPEND: this user's personal numbers ----------------
    print("------------------------ SPEND ------------------------")
    if my_team_spend is not None:
        pct = f" ({my_team_spend / gov_budget * 100:.1f}% of team budget)" if gov_budget else ""
        if has_cycle:
            print(f"Cycle spend    : {money(my_team_spend)}{pct}")
        else:
            print(f"Team spend     : {money(my_team_spend)}{pct}  [no budget cycle — all-time]")
    else:
        label = "Cycle spend" if has_cycle else "Team spend"
        print(f"{label:<14} : unavailable (spend logs not accessible)")
    if lifetime_spend is not None:
        since_str = f" (since {str(created_at)[:10]})" if created_at else ""
        print(f"Lifetime spend : {money(lifetime_spend)}{since_str}")
    else:
        print("Lifetime spend : unavailable (spend logs not accessible)")
    print()

    # --------------------- TEAM BUDGETS --------------------
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
            print(f"      Remaining    : {money2(tbudget - tspend)}")

        else:
            print("      Budget        : unlimited")
            print(f"      Team usage   : {money2(tspend)}")

        cyc = t.get("budget_duration")
        reset = t.get("budget_reset_at")

        if cyc or reset:
            tleft = time_until(reset)

            print(f"      Cycle period : {cyc or '—'}")

            print(
                f"      Resets       : "
                + (
                    f"{reset}" + (f" (in {tleft})" if tleft else "")
                    if reset else "—"
                )
            )

            tstart = cycle_start(reset, cyc)
            if tstart:
                print(f"      Cycle start  : {tstart.isoformat()}")

        else:
            print("      Cycle period : none (no reset)")

    print()
    print(line)

    # --------------------- TEAM MEMBER LIMITS ----------------------
    print("--------------------- TEAM MEMBER LIMITS ----------------------")
    print(f"Member spend            : {money(key_spend)}")

    # 1. Print Budget Info
    if key_budget is not None:
        kpct = (key_spend / key_budget * 100) if key_budget else 0
        print(f"Member budget           : ${key_budget}")
        print(f"Member usage            : {money2(key_spend)} ({kpct:.1f}%)")
        print(f"Member remaining budget : {money(key_budget - key_spend)}")
    else:
        print("Member budget           : unlimited")
        print(f"Member usage            : {money2(key_spend)}")

    # 2. Print Cycle and Reset Info (Independent of whether budget is unlimited)
    if key_cycle or key_reset:
        kcyc, kreset = fmt_reset(key_reset, key_cycle)
        print(f"Cycle period            : {kcyc}")
        print(f"Resets                  : {kreset}")

        kstart = cycle_start(key_reset, key_cycle)
        if kstart:
            print(f"Cycle start             : {kstart.isoformat()}")
    else:
        print("Cycle period            : none (no reset)")

    print()

if __name__ == "__main__":
    main()
