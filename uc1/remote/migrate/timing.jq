# Where a run's time went, from Claude's stream-json (jq -s -r --argjson t0 <epoch> -f timing.jq):
# plain lines, for the log and the report.
# The model's time is everything that is not a command: thinking and writing.
def t: .timestamp | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601;
def d: floor | "\(. / 60 | floor)m\(. % 60 | tostring | if length < 2 then "0" + . else . end)s";
def pct($a; $b): if $b > 0 then "\($a * 100 / $b | round) %" else "-" end;

[.[] | select(.timestamp)] as $e
| ([$e[] | select(.type == "assistant") | t as $ts | .message.content[]? | select(.type == "tool_use")
    | {id, ts: $ts, cmd: (.input.command // "")}]) as $uses
| ([$e[] | select(.type == "user") | t as $ts | .message.content[]? | select(.type == "tool_result")
    | {key: .tool_use_id, value: $ts}] | from_entries) as $done
| (($e | last | t) - $t0) as $total
| ([$uses[] | ($done[.id] // .ts) - .ts] | add // 0) as $cmds
| [$uses[] | select(.cmd | test("^nvpg branch"))] as $branches
| "Claude: \($total | d) in all, \($uses | length) tool calls",
  "  model (thinking, writing): \($total - $cmds | d) (\(pct($total - $cmds; $total)))",
  "  commands: \($cmds | d) (\(pct($cmds; $total)))",
  (if ($branches | length) == 0 then "  no attempt (no nvpg branch)"
   else
     "  analysis, before the first attempt: \($branches[0].ts - $t0 | d)",
     ($branches | to_entries[] |
       (if .key + 1 < ($branches | length) then $branches[.key + 1].ts else $t0 + $total end) as $end
       | "  attempt try\(.key + 1): from +\(.value.ts - $t0 | d), \($end - .value.ts | d)"
         + (if .key + 1 == ($branches | length) then " (with the final migration)" else "" end))
   end)
