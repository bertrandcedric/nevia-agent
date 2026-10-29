# Claude Code's stream-json events, one per input line, in common.jq's vocabulary.
include "common";

def tool:
  .input as $i |
  if .name == "Bash" then "$ \($i.command // "" | command)"
    + (if ($i.description // "") != "" then "\n    # \($i.description | clip(300))" else "" end)
  elif .name == "Edit" or .name == "Write" or .name == "MultiEdit" or .name == "NotebookEdit" then
    "edit \($i.file_path // $i.notebook_path // "")"
  elif .name == "Read" then "read \($i.file_path // "")"
  elif .name == "TodoWrite" then
    "intent: \([$i.todos[]? | select(.status == "in_progress") | .content] | first // "updating its plan" | clip(300))"
  else "\(.name) \($i.pattern // $i.path // $i.url // $i.query // "" | tostring | clip(300))"
  end;

inputs | . as $raw | (try fromjson catch null) as $e |
if ($e | type) != "object" then (select($raw | test("\\S")) | $raw)
elif $e.type == "assistant" then
  $e.message.content[]? |
  if .type == "tool_use" then "\(at) \(tool)"
  elif .type == "text" and (.text | test("\\S")) then "\(at) says: \(.text | message)"
  else empty end
elif $e.type == "user" then
  $e.message.content[]? | select(type == "object" and .type == "tool_result" and .is_error == true) |
  "\(at) failed: \(.content | if type == "array" then (map(.text? // "") | join(" ")) else . end | failure)"
elif $e.type == "result" then
  "\(at) result: \($e.subtype // "?"), \($e.num_turns // "?") turns, $\(($e.total_cost_usd // 0) * 100 | round / 100)"
else empty end
