#!/usr/bin/env bash
#
# usage_report.sh — GenAI gateway usage, incl. personal share of team budgets.
#

GATEWAY="${GATEWAY:-https://gateway.aws.genai.umbc.edu}"
API_KEY="${1:-${API_KEY:-}}"

[[ -z "$API_KEY" ]] && { echo "Usage: $0 sk-your-api-key"; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "Error: jq required."; exit 1; }

auth=(-H "Authorization: Bearer ${API_KEY}")

key_file="$(mktemp)"; user_file="$(mktemp)"; team_file="$(mktemp)"
teams_file="$(mktemp)"; tmp_file="$(mktemp)"; ti_file="$(mktemp)"
trap 'rm -f "$key_file" "$user_file" "$team_file" "$teams_file" "$tmp_file" "$ti_file"' EXIT

# ---- key info ----
# pulls the key to see if it is valid
curl -s "${auth[@]}" "${GATEWAY}/key/info?key=${API_KEY}" > "$key_file"
jq empty "$key_file" >/dev/null 2>&1 || { echo "Non-JSON response:"; cat "$key_file"; exit 1; }
if jq -e '.error // .detail' "$key_file" >/dev/null 2>&1; then
  echo "Gateway error:"; jq -r '.error // .detail' "$key_file"; exit 1
fi

user_id="$(jq -r '.info.user_id // empty' "$key_file")"
key_team_id="$(jq -r '.info.team_id // empty' "$key_file")"

# ---- governing team ----
# gives info of the Top-level team that is assigned to the key
echo '{}' > "$team_file"
if [[ -n "$key_team_id" ]]; then
  curl -s "${auth[@]}" "${GATEWAY}/team/info?team_id=${key_team_id}" > "$team_file"
  jq empty "$team_file" >/dev/null 2>&1 || echo '{}' > "$team_file"
fi

# ---- user info ----
# gives info of the user and its budget if applicable
echo '{}' > "$user_file"
if [[ -n "$user_id" ]]; then
  curl -s "${auth[@]}" "${GATEWAY}/user/info?user_id=${user_id}" > "$user_file"
  jq empty "$user_file" >/dev/null 2>&1 || echo '{}' > "$user_file"
fi

# ---- enumerate all team IDs (handle both string and object entries safely) ----
# lists all team ids that are assigned to key if applicable
team_ids="$(jq -r '
  [ (.teams // [])[]?,
    (.user_info.teams // [])[]? ]
  | map( if type=="object" then (.team_id // empty) else . end )
  | map(select(. != null and . != ""))
  | unique | .[]' "$user_file" 2>/dev/null)"

# ---- fetch each teams budget; accumulate via FILE (no arg-length limit) ----
# gives budget info for each team that the key is assigned to
echo '[]' > "$teams_file"
while IFS= read -r tid; do
  [[ -z "$tid" ]] && continue
  curl -s "${auth[@]}" "${GATEWAY}/team/info?team_id=${tid}" > "$ti_file"
  jq empty "$ti_file" >/dev/null 2>&1 || echo '{}' > "$ti_file"
  jq -n --slurpfile acc "$teams_file" --slurpfile t "$ti_file" --arg id "$tid" \
     '($acc[0] // []) + [ ((($t[0].team_info // $t[0]) // {}) + {resolved_team_id:$id}) ]' \
     > "$tmp_file" && mv "$tmp_file" "$teams_file"
done <<< "$team_ids"

# ---- render ----
jq -n -r \
  --slurpfile keyArr "$key_file" \
  --slurpfile teamArr "$team_file" \
  --slurpfile userArr "$user_file" \
  --slurpfile teamsArr "$teams_file" \
  --arg keyTeam "$key_team_id" \
  --arg uid "$user_id" '

  ($keyArr[0] // {}) as $key |
  ($key.info // {}) as $i |
  ($teamArr[0].team_info // $teamArr[0] // {}) as $t |
  ($userArr[0] // {}) as $u |
  ($teamsArr[0] // []) as $teams |

  # personal spend within a team, guarding against string entries
  def personal_spend($teamObj; $teamId):
    ( [ ((($u.teams // []) + ($u.user_info.teams // []))[]?)
        | select(type=="object")
        | select((.team_id // null) == $teamId)
        | (.spend // .user_spend // empty) ] | first ) as $fromUser
    | ( [ (($teamObj.members_with_roles // [])[]?)
          | select(type=="object")
          | select((.user_id // null) == $uid)
          | (.spend // empty) ] | first ) as $fromTeam
    | ($fromUser // $fromTeam // null);

  ( if $i.max_budget != null then $i.max_budget
    elif $t.max_budget != null then $t.max_budget else null end ) as $budget |
  ( if $i.max_budget != null then ($i.spend // 0)
    elif $t.max_budget != null then ($t.spend // 0)
    else ($i.spend // 0) end ) as $spend |
  ( if $i.max_budget != null then "key"
    elif $t.max_budget != null then "team" else null end ) as $src |
  ( personal_spend($t; $keyTeam) ) as $myShare |

  "======================================================="
  ,"            GenAI Gateway — API Key Usage Report"
  ,"======================================================="
  ,"Key alias      : \($i.key_alias // "—")"
  ,"User / Owner   : \($i.user_id // "—")"
  ,"Team           : \($i.team_id // "—")"
  ,""
  ,"---------------------- SPEND --------------------------"
  ,"Spent          : $\($spend | (.*10000|round)/10000)"
  ,"Budget (max)   : \(if $budget == null then "unlimited" else "$\($budget)\(if $src=="team" then " (team)" else "" end)" end)"
  ,"Remaining      : \(if $budget == null then "unlimited" else "$\((($budget)-($spend))|(.*10000|round)/10000)" end)"
  ,"Usage          : \(if $budget==null or $budget==0 then "—" else "\((($spend)/$budget*1000|round)/10)%" end)"
  ,( if $src=="team" and $myShare != null then
       "Your share     : $\(($myShare)|(.*10000|round)/10000)\(if $budget>0 then " (\((($myShare)/$budget*1000|round)/10)% of team budget)" else "" end)"
     else empty end )
  ,"Models allowed : \(if ($i.models // []) | length == 0 then "all" else ($i.models | join(", ")) end)"
  ,( if ($teams | length) <= 1 then empty
     else
       ( "",
         "------------------ TEAM BUDGETS -----------------------",
# used if the key is assigned to more than one team
         ($teams[] | select(type=="object") |
           (.resolved_team_id) as $tid |
           (personal_spend(.; $tid)) as $mine |
           "  • \(.team_alias // $tid // "—"): "
           + (if .max_budget != null
              then "team $\((.spend // 0)|(.*100|round)/100)/$\(.max_budget)"
              else "team $\((.spend // 0)|(.*100|round)/100) (no budget)" end)
           + (if $mine != null then ", you $\(($mine)|(.*100|round)/100)" else "" end)
           + (if $tid == $keyTeam then "   ← governs this key" else "" end)
         )
       )
     end )
  ,""
  ,"======================================================="
'
