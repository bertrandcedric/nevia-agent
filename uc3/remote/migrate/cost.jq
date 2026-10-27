# What a run cost, from Claude's stream-json and its result (jq -s --slurpfile result <run>.result.json
# --argjson docs <documents migrated> -f cost.jq): JSON, for the report.
# The total and each model's are Claude Code's own (list prices). The attempts are cut as
# timing.jq cuts them, at each nvpg branch; each model's cost is shared out among them in
# proportion to their weighted tokens (input 1, output 5, cache write 1.25, cache read 0.1, the
# ratios of Claude's prices), so the cost of an attempt is an estimate. A message comes once per
# content block in the stream: its usage is counted once, by its id. The calls Claude Code makes
# on its own (another model, e.g. to sum up) are not in the stream: their models' cost is the
# "Claude Code's own calls" row.
def w: (.input_tokens // 0) + 5 * (.output_tokens // 0) + 1.25 * (.cache_creation_input_tokens // 0) + 0.1 * (.cache_read_input_tokens // 0);
def usd: . * 10000 | round / 10000;

($result[0]) as $r
| ($r.modelUsage // {}) as $mu
# Each message once, with the attempt it belongs to: a message that runs `nvpg branch` opens the
# next attempt, from the message after it.
| (reduce (.[] | select(.type == "assistant")) as $e ({phase: "before try1", n: 0, msgs: {}, tools: {}};
    ($e.message.id // "") as $id
    | (if $id == "" or .msgs[$id] then . else .msgs[$id] = {phase, model: $e.message.model} end)
    | (if $id != "" then .msgs[$id].usage = $e.message.usage else . end)
    | reduce ($e.message.content[]? | select(.type == "tool_use" and .name == "Bash")) as $u (.;
        if .tools[$u.id] then . else .tools[$u.id] = true
          | if ($u.input.command // "") | test("^nvpg branch") then .n += 1 | .phase = "try\(.n)" else . end end)
  ) | [.msgs[]]) as $msgs
| ($msgs | group_by(.model) | map({key: .[0].model, value: (map(.usage | w) | add)}) | from_entries) as $wm
| [$msgs[] | . + {cost: (($mu[.model].costUSD // 0) * ((.usage | w) / (if ($wm[.model] // 0) > 0 then $wm[.model] else 1 end)))}] as $priced
| ([$mu | keys[] as $k | select($wm | has($k) | not) | $mu[$k].costUSD // 0] | add // 0) as $own
| {
    total: ($r.total_cost_usd // 0 | usd),
    docs: $docs,
    per_doc: (if $docs > 0 then ($r.total_cost_usd // 0) / $docs else null end),
    models: [$mu | to_entries[] | {model: .key, cost: (.value.costUSD // 0 | usd),
      input: (.value.inputTokens // 0), output: (.value.outputTokens // 0),
      cache_write: (.value.cacheCreationInputTokens // 0), cache_read: (.value.cacheReadInputTokens // 0)}],
    attempts: ([$priced | group_by(.phase)[] | {phase: .[0].phase, messages: length, cost: (map(.cost) | add | usd)}]
      | sort_by(if .phase == "before try1" then 0 else (.phase | ltrimstr("try") | tonumber) end)
      + (if $own > 0 then [{phase: "Claude Code's own calls", messages: null, cost: ($own | usd)}] else [] end))
  }
