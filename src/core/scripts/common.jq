# Shared by both agents' render.jq (`include "common";`, with jq -L on the tools directory): one way to write
# what an AI agent does, one event a line, `HH:MM:SS kind: text`, with the lines under an event indented:
#   intent: what it sets out to do      $ a command (its lines under it)    edit PATH    read PATH
#   says: what it writes               failed: what a tool answered         usage: / result: its counts

def clip($n): tostring | if length > $n then .[0:$n] + " …" else . end;
def head($n): tostring | rtrimstr("\n") | split("\n")
  | if length > $n then .[0:$n] + ["… (\(length - $n) more lines)"] else . end | join("\n");
def indent: gsub("\n"; "\n    ");
def command: head(20) | indent;                    # a shell command, its first 20 lines
def message: clip(2000) | indent;                  # what the agent says
def failure: head(10) | clip(2000) | indent;       # what a failed tool answered
