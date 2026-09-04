#!/usr/bin/env python3
"""GenAI gateway usage report"""

import json
import os
import re
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
        return {"_error": f"HTTP {e.code}: {e.read().decode()[:200]}", "path": path}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"_error": f"timeout/connection error: {e}"}
    except json.JSONDecodeError:
        return {"_error": "non-JSON response"}


# ----------------------------- formatting helpers -----------------------------
def money(v):
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

def fmt_duration(d):
    if d is None:
        return None
    if isinstance(d, (int, float)):
        return f"{int(d)}d"
    s = str(d)
    if s.isdigit():
        return f"{s}d"
    return s

def parse_duration(d):
    if not d:
        return None
    if isinstance(d, (int, float)):
        return timedelta(days=int(d))
    s = str(d)
    if s.isdigit():
        return timedelta(days=int(s))
    try:
        n, unit = int(s[:-1]), s[-1]
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

def cycle_start_floor(reset_raw):
    end = to_dt(reset_raw)
    if end is None:
        return None
    month = end.month - 1
    year = end.year
    if month == 0:
        month = 12
        year -= 1
    return end.replace(year=year, month=month, day=1, hour=0, minute=0,
                       second=0, microsecond=0)

def cycle_days_member(reset_raw):
    end = to_dt(reset_raw)
    start = cycle_start_floor(reset_raw)
    if end and start:
        return (end - start).days
    return None

def next_month_first(reset_raw):
    end = to_dt(reset_raw)
    if end is None:
        return None
    if end.day == 1 and end.hour == 0 and end.minute == 0 and end.second == 0:
        return end
    month = end.month + 1
    year = end.year
    if month == 13:
        month = 1
        year += 1
    return end.replace(year=year, month=month, day=1, hour=0, minute=0,
                       second=0, microsecond=0)

def normalize_model_name(name):
    if not name:
        return name
    n = name.lower().strip()
    if n.startswith("bedrock/"):
        n = n[len("bedrock/"):]
    for prefix in ("us.", "eu.", "ap."):
        if n.startswith(prefix):
            n = n[len(prefix):]
    if n.startswith("anthropic."):
        n = n[len("anthropic."):]
    n = re.sub(r"-\d{8}-v\d+(?::\d+)?$", "", n)
    n = re.sub(r"-v\d+(?::\d+)?$", "", n)
    n = n.replace(" ", "-").replace(".", "-")
    n = re.sub(r"-+", "-", n)
    return n

def merge_by_canonical(spend_dict):
    merged = {}
    for model, spend in spend_dict.items():
        merged[normalize_model_name(model)] = merged.get(normalize_model_name(model), 0.0) + spend
    return merged


# ----------------------------- spend via /spend/logs (original fallback) -----------------------------
def fetch_user_logs(token, uid):
    resp = api_get(f"/spend/logs?user_id={uid}", token)
    if isinstance(resp, dict) and "_error" in resp:
        return [], False
    rows = resp if isinstance(resp, list) else resp.get("data", resp.get("logs", []))
    if not isinstance(rows, list):
        return [], False
    return rows, True

def sum_logs(rows, team_id=None, since_dt=None):
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

def sum_logs_by_model(rows, team_id=None, since_dt=None):
    totals = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        if team_id is not None and r.get("team_id") != team_id:
            continue
        ts = to_dt(r.get("startTime") or r.get("created_at") or r.get("timestamp"))
        if since_dt and ts and ts < since_dt:
            continue
        model = r.get("model") or "unknown"
        if not model or str(model).lower == "unknown":
            continue
        totals[model] = totals.get(model, 0.0) + (r.get("spend", 0) or 0)
    return totals


# ----------------------------- spend via /spend/report (new, if available) -----------------------------
def spend_report(token, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    resp = api_get(f"/spend/report?{qs}", token)
    if isinstance(resp, dict) and "_error" in resp:
        return [], False
    rows = resp if isinstance(resp, list) else resp.get("data", resp.get("results", []))
    return (rows if isinstance(rows, list) else []), True

def total_from_report(rows):
    return sum(r.get("total_spend") or r.get("spend") or r.get("total") or 0 for r in rows)


# ------------------------------- main -------------------------------
def main():
    api_key = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("API_KEY")
    if not api_key or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print("\nOptions:")
        print("  -h, --help       Show this help message and exit")
        print("\nDescription:")
        print("  Tracks Open WebUI/LiteLLM budget usage for users")
        print("\nEnvironment variables (optional):")
        print("  API_KEY          Your gateway API key (alternative to passing as argument)")
        print("\nExamples:")
        print("  python3 report.py sk-your-api-key")
        print("  API_KEY=sk-your-api-key python3 report.py")
        sys.exit(0 if sys.argv[1] in ("-h", "--help") else 1)

    # ---- key info ----
    key_resp = api_get(f"/key/info?key={api_key}", api_key)
    if "_error" in key_resp:
        sys.exit(f"Error fetching key info: {key_resp['_error']}")
    if "error" in key_resp or "detail" in key_resp:
        sys.exit(f"Gateway error: {key_resp.get('error') or key_resp.get('detail')}")

    info = key_resp.get("info", {})
    uid = info.get("user_id")
    user_email = info.get("user_email") or info.get("email")
    key_team_id = info.get("team_id")
    key_spend = info.get("spend") or 0
    created_at = info.get("created_at")
    if uid:
        ur = api_get(f"/user/info?user_id={uid}", api_key)
        user = ur if "_error" not in ur else {}
        user_info = user.get("user_info", user)
        created_at = user_info.get("created_at") or created_at

    # ---- available models ----
    models_resp = api_get("/v1/models", api_key)
    available_models = [m.get("id") for m in (models_resp.get("data") or []) if m.get("id")]

    # ---- governing team: try /v2/team/info, fall back to /team/info ----
    team = {}
    gov_budget = None
    has_cycle = False
    if key_team_id:
        tr = api_get(f"/v2/team/info?team_id={key_team_id}", api_key)
        if "_error" in tr:
            tr = api_get(f"/team/info?team_id={key_team_id}", api_key)
        team = tr.get("team_info", tr) if "_error" not in tr else {}
        gov_budget = team.get("max_budget")
        has_cycle = bool(team.get("budget_duration") or team.get("budget_reset_at"))

    # ---- team-member budget ----
    member_bt = team.get("team_member_budget_table") or {}
    if isinstance(member_bt, dict) and member_bt.get("max_budget") is None and key_team_id:
        mr = api_get(f"/team/{key_team_id}/members/me", api_key)
        if "_error" not in mr and isinstance(mr, dict):
            member_bt = mr.get("litellm_budget_table") or {}
    key_budget = member_bt.get("max_budget") if isinstance(member_bt, dict) else None
    key_cycle = (member_bt.get("budget_duration") or member_bt.get("duration")) if isinstance(member_bt, dict) else None
    key_reset = (member_bt.get("budget_reset_at") or member_bt.get("reset_at")) if isinstance(member_bt, dict) else None
    if key_budget is None:
        key_budget = team.get("max_budget")
    if key_cycle is None:
        key_cycle = team.get("budget_duration")
    if key_reset is None:
        key_reset = team.get("budget_reset_at")

    # ---- enumerate teams: try /v2/team/list (or /team/list) once ----
    team_ids = set([key_team_id] if key_team_id else [])
    teams = []
    teams_resp = api_get("/v2/team/list", api_key)
    if "_error" in teams_resp:
        teams_resp = api_get("/team/list", api_key)
    if "_error" not in teams_resp:
        for t in (teams_resp.get("data") or teams_resp.get("teams") or []):
            if isinstance(t, dict):
                t["resolved_team_id"] = t.get("team_id")
                teams.append(t)
    # fallback: loop per team id (original behaviour)
    if not teams:
        for tid in sorted(team_ids):
            tr = api_get(f"/team/info?team_id={tid}", api_key)
            obj = tr.get("team_info", tr) if "_error" not in tr else {}
            obj["resolved_team_id"] = tid
            teams.append(obj)

    # ---- compute the cycle window once ----
    correct_reset = next_month_first(key_reset) if key_reset else None
    since = cycle_start_floor(correct_reset) if correct_reset else None
    if since and since > datetime.now(tz=timezone.utc):
        since = None
    team_since = cycle_start(team.get("budget_reset_at"), team.get("budget_duration")) if key_team_id else None

    # ---- spend: prefer /spend/report, fall back to /spend/logs ----
    my_team_spend = my_member_spend = lifetime_spend = None
    model_cycle_spend = {}
    model_lifetime_spend = {}

    report, r_ok = spend_report(api_key)
    if r_ok and report:
        # server did the grouping; lifetime = total of all rows
        lifetime_spend = total_from_report(report)
        # per-model lifetime: group by 'model' key if present
        by_model = {}
        for r in report:
            mdl = r.get("model") or r.get("model_id")
            if mdl:
                by_model[mdl] = by_model.get(mdl, 0.0) + (r.get("total_spend") or r.get("spend") or r.get("total") or 0)
        model_lifetime_spend = merge_by_canonical(by_model)

        # member / team cycle spend: request with team_id filter + start_date if supported
        if key_team_id:
            start_s = since.strftime("%Y-%m-%d") if since else None
            cyc_report, c_ok = spend_report(api_key, team_id=key_team_id, start_date=start_s)
            if c_ok:
                my_member_spend = total_from_report(cyc_report)
                c_by_model = {}
                for r in cyc_report:
                    mdl = r.get("model") or r.get("model_id")
                    if mdl:
                        c_by_model[mdl] = c_by_model.get(mdl, 0.0) + (r.get("total_spend") or r.get("spend") or r.get("total") or 0)
                model_cycle_spend = merge_by_canonical(c_by_model)
            # team cycle spend: report without start_date = current cycle by team
            my_team_spend = my_member_spend
        else:
            my_team_spend = my_member_spend = lifetime_spend

    if my_member_spend is None and uid:
        # ---- fallback: original /spend/logs + local summing ----
        log_rows, logs_ok = [], False
        for _ in range(3):
            log_rows, logs_ok = fetch_user_logs(api_key, uid)
            if logs_ok:
                break
            time.sleep(2)
        if logs_ok:
            my_team_spend = sum_logs(log_rows, team_id=key_team_id, since_dt=team_since)
            my_member_spend = sum_logs(log_rows, team_id=key_team_id, since_dt=since)
            lifetime_spend = sum_logs(log_rows, team_id=None, since_dt=None)
            model_cycle_spend = merge_by_canonical(sum_logs_by_model(log_rows, team_id=key_team_id, since_dt=since))
            model_lifetime_spend = merge_by_canonical(sum_logs_by_model(log_rows, team_id=None, since_dt=None))

    models = info.get("models") or []

    # ============================ render (identical to original) ============================
    line = "=" * 55
    print(line)
    print("            GenAI Gateway — API Key Usage Report")
    print(line)
    print(f"Key alias      : {info.get('key_alias') or '—'}")
    print(f"User           : {info.get('user_id') or '—'}")
    print(f"Team           : {info.get('team_id') or '—'}")
    print(f"Models allowed : {'all' if not models else ', '.join(models)}")
    print()

    print("------------------------ SPEND ------------------------")
    if my_team_spend is not None:
        pct = f" ({my_team_spend / gov_budget * 100:.1f}% of team budget)" if gov_budget else ""
        if has_cycle:
            print(f"Cycle spend    : {money(my_team_spend)}{pct}  [personal spend per current team cycle]")
        else:
            print(f"Team spend     : {money(my_team_spend)}{pct}  [no budget cycle - all-time]")
    else:
        label = "Cycle spend" if has_cycle else "Team spend"
        print(f"{label:<14} : spend logs not accessible (run the script again)")
    if lifetime_spend is not None:
        since_str = f" (since {str(created_at)[:10]})" if created_at else ""
        print(f"Lifetime spend : {money(lifetime_spend)}{since_str}  [all user keys - all-time]")
    else:
        print("Lifetime spend : spend logs not accessible (run the script again)")
    print()

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
            print(f"      Budget       : {money(tbudget)}")
            print(f"      Team usage   : {money(tspend)} [whole team current cycle spend]")
            print(f"      Remaining    : {money(tbudget - tspend)}")
        else:
            print("      Budget        : unlimited")
            print(f"      Team usage   : {money(tspend)}")
        cyc = t.get("budget_duration")
        reset = t.get("budget_reset_at")
        if cyc or reset:
            tleft = time_until(reset)
            print(f"      Cycle period : {cyc or '—'}")
            print(
                f"      Resets       : "
                + (f"{reset}" + (f" (in {tleft})" if tleft else "") if reset else "—")
            )
            tstart = cycle_start(reset, cyc)
            if tstart:
                print(f"      Cycle start  : {tstart.isoformat()}")
        else:
            print("      Cycle period : none (no reset)")
    print()

    print("--------------------- TEAM MEMBER LIMITS ----------------------")
    display_spend = my_member_spend if my_member_spend is not None else key_spend
    if key_budget is not None:
        kpct = (display_spend / key_budget * 100) if key_budget else 0
        print(f"Member spend            : {money(key_spend)} [current personal key - all-time]")
        print(f"Member budget           : {money(key_budget)}")
        print(f"Member usage            : {money(display_spend)} [current personal key cycle spend]")
        print(f"Member remaining budget : {money(key_budget - display_spend)}")
    else:
        print(f"Member spend            : {money(key_spend)}")
        print("Member budget           : unlimited")
        print(f"Member usage            : {money(display_spend)}")
    if key_cycle or key_reset:
        correct_reset = next_month_first(key_reset)
        monthly_cycle = cycle_days_member(correct_reset) if correct_reset else None
        kcyc = f"{monthly_cycle}d" if monthly_cycle else (fmt_duration(key_cycle) or "none (no reset)")
        tleft = time_until(correct_reset)
        kreset = f"{correct_reset.isoformat()}" + (f" (in {tleft})" if tleft else "") if correct_reset else "—"
        print(f"Cycle period            : {kcyc}")
        print(f"Resets                  : {kreset}")
        kstart = cycle_start_floor(correct_reset)
        if kstart:
            print(f"Cycle start             : {kstart.isoformat()}")
    else:
        print("Cycle period            : none (no reset)")
    print()

    print("--------------------- MODEL USAGE ----------------------")
    print("  (Models used by key)")
    print()
    all_models_to_show = set(normalize_model_name(m) for m in available_models) | set(model_cycle_spend) | set(model_lifetime_spend)
    if not all_models_to_show:
        print("  No model usage data available.")
    else:
        for model in sorted(all_models_to_show):
            if model.lower() == "unknown":
                continue
            cycle_val = model_cycle_spend.get(model, 0.0)
            lifetime_val = model_lifetime_spend.get(model, 0.0)
            if cycle_val == 0 and lifetime_val == 0:
                continue
            print(f"  - {model}")
            print(f"        Cycle spend   : {money(cycle_val)}")
            print(f"        Lifetime spend: {money(lifetime_val)}")
    print()


if __name__ == "__main__":
    main()
