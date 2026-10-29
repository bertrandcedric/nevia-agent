# nevia-agent

Two agents on Nevia, in one project: **m2p** (`src/m2p/`) works out how to load the hello MongoDB
into PostgreSQL with mongo2pg; **acc** (`src/acc/`) changes an app (its migration to Postgres, or
a feature) and opens a pull request once a sandbox has passed. The only local commands are `agent m2p ...`
and `agent acc ...`: `setup`, `run`, `status`, `stop`, `clean`, `destroy`. README.md has them, docs/ the detail.

## Working here

- Python 3.15 with uv: `uv sync`, then `uv run ruff format`, `uv run ruff check`, `uv run ty check` and
  `uv run pytest` before a commit (the prek hooks, `uv run prek install`, run ruff and ty too). No just.
- The CLI is `uv run agent`. Settings and pinned versions are in `agent.toml`: `[runtime]`, `[ai]`, `[mongodb]`
  and `[mongo2pg]` shared, then `[m2p.*]` and `[acc.*]`, each with its own `[<agent>.nevia] workspace`; it
  holds no secret, only the gateway key's
  env var. Keep in it only what someone may choose: conventions (secret names, users, paths) are ClassVars
  or constants in the code. Never hard-code Nevia names or
  versions in code. Each agent has its own workspace (`[<agent>.nevia]`, made by `Bootstrap.up` if need be);
  their Nevia names still must not collide, should they share one.
- Secrets come from `.env` (ignored by git; `.env.example` lists them), which `agent` loads at each command
  without overriding the shell. Add a secret an agent needs to `.env.example`.
- The code is laid out by where it runs; keep it so (`tests/test_layers.py` checks it):
  - **your machine**: `core/local/` (the `agent` CLI, `bootstrap`, `runtime`, `collect`, `.env`) and each
    agent's `local.py` (its commands: make the agent computer, run there, follow, collect);
  - **the agent computer**: `core/agent/` (mongo2pg's `Builder`, the prompts) and each agent's `agent.py`
    (its work, and the hidden `remote` commands `local.py` calls), with m2p's `usecase/` and acc's
    `stack.py` and `pr.py`; none of it imports `core.local` or a `local` module;
  - **any Nevia computer**: the bash and jq in `core/scripts/` (both agents') and `<agent>/scripts/`, only where
    Python can't be: the MongoDB and app computers, the runtime's setup, and the AI agents' own tools
    (`branch.sh`, `render-<engine>.jq`). On the agent computer, the work is Python (`<agent>/steps.py`): a subprocess
    here, or a job (`agent <agent> remote step ...`) when it needs a secret, the one way its template gets a
    value. `Computer` runs commands and writes files locally when it is the computer itself (`here`);
  - **both sides**: `core/config.py`, `core/computer.py` (`Computer` and its jobs; each agent's
    `computer.py` names its scripts and jobs dir), `core/activity.py`, `core/shell.py`.
  Put what both agents need in core, not in one of them.
- Nevia goes through the `nevia` CLI, `core.nevia.Nevia` (`--json`, the API's documents): yours on your machine
  (`nevia login`), the agent computer's (`[runtime] nevia`, its key file) there, for the code and the AI alike.
  Only the tunnels go through [nevia-kit](https://github.com/aiven/nevia-kit) (`Nevia.kit`), pinned to a release
  tag in `pyproject.toml` (`[tool.uv.sources]`): the CLI has none. Add what the code needs to `core.nevia`. The one local command,
  curl for mongo2pg's release binary, goes through `core.shell.Runner`.
- Tests use nevia-kit's `FakeApi` (`validate=True`) behind a fake `nevia` (`tests/fake_nevia.py`, which reads the
  command lines as the CLI does) and never reach Nevia, podman, git, GitHub or the network:
  `tests/m2p/`, `tests/acc/`, and `tests/` for core.
- Scripts run on Nevia computers (Debian 13, bash) and source `~/.nevia-agent/lib.sh` (`core/scripts/lib.sh`:
  messages, `apt_install`, `fetch_checked`, installers); what both agents run is in `core/scripts/`:
  `mongo-host.sh` and `mongo-up.sh` (MongoDB in a Podman pod, `[mongodb]`; auth, init scripts and chisel
  for an app's), and `render-claude.jq` and `render-copilot.jq`, which include `common.jq` so that both
  engines' logs read alike
  (`core.activity` parses them). Add to these rather than to one agent.
- Long remote work runs through `Computer.start_job`/`wait_job`/`run_job` (nohup plus polling), because Nevia
  exec sessions drop.
- mongo2pg is always `mongo2pg.release`'s binary, never built, here or on an agent computer.
- Every prompt is a file in `prompts/<agent>/`, read with `core.agent.prompts.prompt`, one per task and in the
  same shape (the job, `## What is here`, how to check, `## How to work`, the expected output as a template,
  `## Rules`): m2p's `AGENTS.md`, each AI session's prompt (filled with `str.format`, so literal braces
  are doubled), acc's `MIGRATE.md` and `FEATURE.md` (`{{REPO}}`, `{{BRANCH}}`, `{{RUN_DIR}}`, `{{CHECK}}`,
  then `{{TASK}}` replaced as text). Keep prompt text out of code.

### m2p

- `m2p/usecase/` holds what is specific to the hello data: `seed.py`, which generates it, `DESCRIPTION`, its
  words in the agent's guide (`{data}` in AGENTS.md), and the `[m2p.seed]` settings. The rest works on any
  MongoDB database: keep data specifics out of it.
- `usecase/seed.py` is a port of uc3's `seed.mjs` and must give the same documents for the same settings:
  keep the order in which values are drawn (`tests/m2p/usecase/test_seed.py` pins uc3's numbers).
- The agent loop's computer gets the guide (`prompts/m2p/AGENTS.md`) and the read-only tools in
  `m2p/scripts/`. Keep hints about the known fixes out of them; the agent has to find those itself. A load is checked by mongo2pg's post-import report only.
- The agent fixes the data first (configuration, or one `normalize.sql` run after the import) and changes the
  generated DDL only when it must. There is no local MongoDB and no local migration: don't add them back.

### acc

- The agent computer drives a run (`acc/agent.py`): it makes the databases, checkpoints and sandboxes with
  the nevia CLI, and runs the long work as jobs. Each step is recorded in the run's `state.json` once done, so a
  run can be taken up: keep steps safe to redo.
- `acc/steps.py` is a run's work (clone, analysis, the AI as its own user, build, seed data, pull request,
  setup); `acc/scripts/` holds `app-up.sh`, the app computer's.

## Rules

- Never print or log a Postgres or MongoDB URI. It moves from Nevia's API to a workspace secret, and reaches a
  computer through its env (m2p's agent, acc's app) or a per-command env var (acc's jobs), or a mode-600 file
  on a computer; `redact` scrubs job logs.
- The AI gateway key and the GitHub token go from `.env` into their workspace secrets and reach an agent
  computer only as per-command env vars on the jobs that need them. Never print them or put them in a
  computer's env.
- m2p: agent key files are credentials: they go into the `agent.secret` workspace secret and reach a computer
  only through a per-command exec env var. Never print them, put them in a script, or store them in a
  computer's env.
- Your machine makes only the agent computer, its principal and key, and the secrets from `.env`
  (`core.bootstrap`), sends it this code (`core.runtime`), follows it by exec, and deletes those at the end.
  Everything else runs there, through the hidden `agent <m2p|acc> remote ...` commands: keep it so.
- acc: the AI runs as its own Unix user (`acc.config.AI_USER`), with the run's directory lent to it, and the
  `nevia` CLI with a copy of the agent's key (`steps.lend_nevia`): it can't read the orchestrator's env or its
  jobs' secrets. Its prompts let it look at Nevia, not change it. The AI sees the structure of the data, never its
  values: `steps.analyse` strips the sample values and deletes the mongo2pg project (its config holds the URIs).
  Every Nevia name an agent makes carries it: resources `m2p-`/`acc-` (checked by `AgentSettings`), secrets
  `M2P_`/`ACC_` (always through `Settings.secret`).
- `clean` and `destroy` ask first, and `destroy` lists what it deletes: keep it so. They match only the names
  in agent.toml and the names made from them (m2p's agent principal by its name and description; acc's
  `<name>-<run>`, `<SECRET>_<run>`, `acc-<run>`).
- Large transfers and remote TCP access go through `Tunnels`, never a published port straight to a service,
  except acc's app to its MongoDB, through chisel, because that link outlives any run. A tunnel's secret stays
  in env vars, its remote processes stop after `lifetime`, and closing it removes its port mapping. Port
  changes go through `nv.computers.publish`/`unpublish`, which keep the other mappings.
- Nevia computers have 2 GiB and no swap: cap Mongo's cache, and the app's heap.
- The remote is `bertrandcedric/nevia-agent`; push only when asked. Commits follow Conventional Commits; keep
  CHANGELOG.md under `[Unreleased]`.
