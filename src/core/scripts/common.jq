# Shared by render-claude.jq and render-copilot.jq (`include "common";`, with jq -L on the tools directory): one
# way to write what an AI agent does, whichever it is, one event a line, `HH:MM:SS kind: text` (the time in TZ),
# with the lines under an event indented:
#   intent: what it sets out to do      $ a command (its lines under it)    edit PATH    read PATH
#   says: what it writes               failed: what a tool answered         result: how it ended
# Lines that are not JSON (its stderr) pass through as they are. Run as: jq --unbuffered -nRr -L TOOLS -f FILE

def at: now | localtime | strftime("%H:%M:%S");

def clip($n): tostring | if length > $n then .[0:$n] + " …" else . end;
def head($n): tostring | rtrimstr("\n") | split("\n")
  | if length > $n then .[0:$n] + ["… (\(length - $n) more lines)"] else . end | join("\n");
def indent: gsub("\n"; "\n    ");
def command: head(20) | indent;                    # a shell command, its first 20 lines
def message: clip(2000) | indent;                  # what the agent says
def failure: head(10) | clip(2000) | indent;       # what a failed tool answered
