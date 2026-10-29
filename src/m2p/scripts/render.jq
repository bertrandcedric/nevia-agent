# Copilot's --output-format json events, one per input line, as readable lines for logs/session-N.log:
#   HH:MM:SS intent: Creating try 2
#   HH:MM:SS $ psql ... (continuation lines indented)
#   HH:MM:SS failed: what the tool answered
# Lines that are not JSON (Copilot's stderr) pass through as they are.
# Token counts come out as `usage:` lines, which `agent m2p run` reads for the cost.

include "common";
def counts: "input=\(.inputTokens // .input_tokens // .prompt_tokens // 0)"
  + " cached=\(.cacheReadTokens // .cache_read_tokens // .cached_tokens // 0)"
  + " output=\(.outputTokens // .output_tokens // .completion_tokens // 0)";

def render:
  (.timestamp // "" | tostring | .[11:19]) as $at |
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
  elif .type == "assistant.usage" then "\($at) usage: model=\($d.model // "") \($d | counts)"
  elif ($d.modelMetrics | type) == "object" then
    $d.modelMetrics | to_entries[] | (.value | if type == "object" then . else {} end) as $m
    | "usage total: model=\(.key) requests=\($m.requests.count // 0) \($m.usage // $m | counts)"
  elif .type == "result" then
    "\($at) result: exit \(.exitCode // "?"), \((.usage.sessionDurationMs // 0) / 1000 | floor) s"
    + (.usage.codeChanges.filesModified // [] | if length > 0 then ", changed \(join(", "))" else "" end)
  else empty
  end;

. as $line | (try fromjson catch null) as $event
| if ($event | type) == "object" then $event | render else $line end
