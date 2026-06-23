#!/usr/bin/env python3
"""GenAI gateway usage report — no jq required (stdlib only)."""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

GATEWAY = os.environ.get("GATEWAY", "https://gateway.aws.genai.umbc.edu")


def api_get(path, token):
    """GET a gateway endpoint, return parsed JSON or {} on failure."""
    url = f"{GATEWAY}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        return {"_error": f"HTTP {e.code}: {body}"}
    except urllib.error.URLError as e:
        return {"_error": f"Connection error: {e.reason}"}
    except json.JSONDecodeError:
        return {"_error": "non-JSON response"}


def money(v):
    return f"${(v or 0):.4f}"


def money2(v):
    return f"${(v or 0):.2f}"


def time_until(reset_raw):
    """Return 'Xd Yh' / 'Yh Zm' / 'Zm' until reset, or None."""
    if reset_raw is None:
        return None
    if isinstance(reset_raw, (int, float)):
        target = float(reset_raw)
    else:
        s = str(reset_raw).replace("Z", "").split(".")[0]
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            target = dt.timestamp()
        except ValueError:
            return None
    secs = max(0, int(target - time.time()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def personal_spend(user, team_obj, uid, team_id):
    """This user's spend within a given team, if exposed; else None."""
    # (1) /user/info teams[] entry for this team
    teams = (user.get("teams") or []) + (user.get("user_info", {}).get("teams") or [])
    for t in teams:
        if isinstance(t, dict) and t.get("team_id") == team_id:
            v = t.get("spend", t.get("user_spend"))
            if v is not None:
                return v
    # (2) team members_with_roles[] entry for this user
    for m in (team_obj.get("members_with_roles") or []):
        if isinstance(m, dict) and m.get("user_id") == uid:
            if m.get("spend") is not None:
                return m["spend"]
    return None


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

    # ---- user info (for all team memberships) ----
    user = {}
    if uid:
        ur = api_get(f"/user/info?user_id={uid}", api_key)
        user = ur if "_error" not in ur else {}

    # ---- all teams the user belongs to ----
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

    my_share = personal_spend(user, team, uid, key_team_id) if src == "team" else None
    tleft = time_until(reset_raw)
    models = info.get("models") or []

    # ---- render ----
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
        pct = (spend / budget * 100) if budget else 0
        print(f"Usage          : {pct:.1f}%")
    if cycle:
        print(f"Budget cycle   : {cycle}")
    if reset_raw:
        suffix = f" (in {tleft})" if tleft else ""
        print(f"Budget resets  : {reset_raw}{suffix}")
    if src == "team" and my_share is not None:
        sh = f" ({my_share / budget * 100:.1f}% of team budget)" if budget else ""
        print(f"Your share     : {money(my_share)}{sh}")
    print(f"Models allowed : {'all' if not models else ', '.join(models)}")

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
            mine = personal_spend(user, t, uid, tid)
            you = f", you {money2(mine)}" if mine is not None else ""
            gov = "   ← governs this key" if tid == key_team_id else ""
            print(f"  • {name}: {budget_str}{you}{gov}")
    print()
    print(line)


if __name__ == "__main__":
    main()