# Guidelines for a presentation

What we settled on while building [presentation.md](presentation.md) and its PowerPoint (`make_pptx.py`), to
follow for the next one.

## Content

- **True to the code**: check every step, name and claim against what the code really does before showing it.
- **Only what the audience needs**: show what a step does and why, not the internals that serve it (a helper
  database, a build command, a script's name); those go in the speaker's notes if anywhere.
- **Say what each name is** the first time it appears: an agent, a tool (e.g. "**m2p**, Mongo to Postgres: the
  agent that migrates the data").
- **Show each step's inputs** (the AI changes the code from the clone *and* the analysis) and **what is left at
  the end** (both apps running, to compare side by side).

## Text

- **Concise**: one short line per bullet, the idea in bold and a few words after it; the detail goes in the
  speaker's notes, not on the slide.
- **Plain words in the captions**: no resource names, file names or commands; say what happens and why
  ("a fresh copy of the target", not "`m2p-try2`, a branch made by `branch.sh 2`"). Prefer the general case
  to an invented example.
- **Spread out**: a text-only slide has its title at the top and its bullets spread over the rest of the height
  (in Marp, `<!-- _class: text -->` with the deck's `section.text` style), not packed in the middle.

## Diagrams

- **One picture for the big idea** (here, the triangle: the app at the top, the source and the target at the
  base), with arrows that show where each part acts, and what they all start from (the assessment).
- **One clean, modern style for every diagram** (as in `presentation_harmonized_v3.pptx`):
  - every box a rounded rectangle (13 % corners), dark, outlined (1 pt) in the colour of its kind: white your
    machine, teal compute, green data, purple AI, orange result; a small legend, top right, names them;
  - the name in bold over a grey detail, centred, and every name fitting its box;
  - thin grey lines, straight or elbowed, no curves, never crossing a box; a line into a box and one out of it
    don't share a path;
  - a status as a small badge in a box's top right corner (FAILS, PASSES, LOADED, LIVE);
  - no glow, no shadow.
- Draw them as native shapes in the PowerPoint (one script, `make_pptx.py`), as an SVG or Mermaid in the
  Markdown; the Markdown stays the source of the text, captions included.

## Animations

- **A timeline, from left to right**: one column per step, a time axis under the diagram (a dot and a label per
  step: done, current in green, still to come); everything a step adds sits in its column.
- **A box never changes colour from one step to the next**: the step being shown has its arrows and its dot on
  the axis in green; what is still to come is not drawn yet.
- **Show repetition**: when the work loops, each round gets its own boxes (try, then analyse and fix, then the
  next try), so that each new round is seen appearing.
- **Show the outcome where it lands**: the last step marks what it changed (the target, LOADED; the apps,
  LIVE) at the same time as the result appears.
- **One slide per step**, no transition, no fade: going back a step is going back a slide, in any app or a PDF.
  Each step's caption is its slide's notes.

## The file

- Keep only the template's layouts the deck uses: Keynote warns about the others ("unsupported media").
- The brand fonts (Inter, Geist Mono, Funnel Display) must be installed where the deck is opened; after
  installing them, restart Keynote or PowerPoint.

## Working on it

- Show previews (rendered images) of a few styles before changing the deck, then apply the one chosen.
- Rebuild the deck after each change, and check its layout (overlaps, text past its box, lines through boxes)
  on a render.
