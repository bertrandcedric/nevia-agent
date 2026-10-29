# GitHub Copilot CLI's --output-format json events, one per input line, in common.jq's vocabulary.
include "common";

def render:
  # The event's own time (ISO 8601, UTC) in TZ, as `at` writes it.
  (try (.timestamp[0:19] + "Z" | fromdateiso8601 | localtime | strftime("%H:%M:%S")) catch at) as $at |
  (.data // {} | if type == "object" then . else {} end) as $d |
  if .type == "tool.execution_start" then
    ($d.arguments // {} | if type == "object" then . else {} end) as $a |
    if $d.toolName == "apply_patch" then
      # The patch is the argument itself; its file lines say what it changes.
      "\($at) edit \($d.arguments | tostring | [scan("\\*\\*\\* (Add|Update|Delete) File: ([^\n]+)") | "\(.[1]) (\(.[0] | ascii_downcase))"]
        | join(", ") | clip(300))"
    elif $d.toolName == "report_intent" then "\($at) intent: \($a.intent // "" | clip(300))"
    elif $d.toolName == "bash" then
      "\($at) $ \($a.command // "" | command)"
      + (if ($a.description // "") != "" then "\n    # \($a.description | clip(300))" else "" end)
    else "\($at) \($d.toolName // "tool") \($a.path // $a.pattern // $a.query // $a.intent // $a.shellId // "" | clip(300))"
    end
  elif .type == "tool.shell_output" then
    ($d.text // "") | select(. != "") | "    " + (head(20) | clip(2000) | indent)
  elif .type == "tool.execution_complete" then
    select($d.success == false)
    | "\($at) failed: \($d.result.content // $d.error.message // $d.error // "" | failure)"
  elif .type == "assistant.message" then
    ($d.content // "") | select(. != "") as $c | "\($at) says: \($c | message)"
  elif .type == "system.notification" then
    if $d.kind.type == "shell_completed" then
      "\($at) background command \"\($d.kind.description // $d.kind.shellId // "")\" ended with \($d.kind.exitCode)"
    else "\($at) note: \($d.content // "" | gsub("</?system_notification>"; "") | clip(300) | indent)"
    end
  elif .type == "user.message" then "\($at) prompt: \($d.content // "" | clip(500) | indent)"
  elif .type == "session.error" then "\($at) error: \($d.message // "" | clip(1000) | indent)"
  elif .type == "result" then
    "\($at) result: exit \(.exitCode // "?"), \((.usage.sessionDurationMs // 0) / 1000 | floor) s"
    + (.usage.codeChanges.filesModified // [] | if length > 0 then ", changed \(join(", "))" else "" end)
  else empty
  end;

inputs | . as $line | (try fromjson catch null) as $event |
if ($event | type) == "object" then $event | render else (select($line | test("\\S")) | $line) end
