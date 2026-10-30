# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `status`, both agents': Nevia's operations still running in the workspace (`nevia ops -f state=running`,
  `Nevia.operations`), a line each with its kind, its resource and since when; nothing when there is none.
- `[acc.postgres] pooler` (false by default): the app's `DATABASE_URL`, the stack's and the sandbox's, through the
  database's connection pooler (`nevia pg connect --pooled`, port 6432, transaction pooling) with
  `prepareThreshold=0` for JDBC; the seed data's psql drops that parameter, the AI's own Postgres stays direct.
- `agent --workspace NAME` (or `AGENT_WORKSPACE`): the Nevia workspace instead of agent.toml's, for every
  command, passed on to the agent computer's own commands and jobs.
- A GitHub Actions workflow, `.github/workflows/ci.yml`: ruff, `bash -n` and the TOML files on every push to
  main and every pull request, without Nevia or nevia-kit; ty and pytest stay local.
- m2p's run ends by resetting the passwords of the target and of every try branch still there (`nevia pg
  reset-password`), however it ended (`stop` and `agent.hours` too): the URIs the AI saw stop working; the
  target's new one goes into `M2P_PG_URL`, and the `tries/m2p-tryN.uri` files are deleted.
- After an m2p run that passed (DONE.md), the target is protected and copied into `<postgres.name>-loaded`,
  protected too, in place of an older copy: a restore point before the cut-over. `clean` keeps both; `destroy`
  lists them, with the protection it clears first (`Doomed.unprotected`, `Databases.protect`).
- docs/m2p.md: Nevia's Postgres has a fixed size and 20 GiB of storage, the ceiling for `[m2p.seed] customers`.
- m2p collects the AI's cost: each session's result in `logs/ai-N.json` and its cost on its end line, the run's
  total in `result.json` and `report.md` (as acc's), and `run` prints it after collecting.

### Changed

- The `nevia` CLI's sign-in limit (30 token requests a minute from one address, a key file taking one per
  command): a refused call waits the time the CLI gives and is sent again (`core.nevia.THROTTLED`), and m2p's
  `status` lists its try branches at most every 30 s.
- acc's sandbox waits for its DNS in one command on it (`Stack.wait_dns`), not one exec every 2 s: up to 30
  sign-ins a minute alone, which failed run 261030173011's next `nevia set`.
- m2p: mongo2pg's `infer` runs once, by the pipeline, at the start of a run (`steps.model`, into a read-only
  `tries/model/`, with `[mongo2pg] sample_percent` as the only change to `init`'s config), before the first
  session; the guide tells the AI never to run `infer` nor change the sampling, and each try starts from the
  model or the try before's (`source/`, `schema/`, `to-pg` after a mapping change). Runs 261030170535 and
  261030172225 inferred again for each try, the last with `percent = 100`.
- acc's AI signs its `nevia` CLI in with a viewer's key of its own, `<agent.computer>-ai` (`ACC_NEVIA_AI_KEY`),
  instead of a copy of the agent's editor key: it can list and inspect, not change anything, `exec` or read a
  database's connection. Your machine makes it at `setup` if it is missing (only an admin may) and removes it
  at `destroy`; `setup` deletes the copy of the agent's key an earlier setup lent. The prompts no longer offer
  `nevia exec`.
- The agent computers' nevia CLI (pinned, `[runtime] nevia`) runs with `NEVIA_NO_UPDATE_CHECK=1`: in
  `bin/agent` and setup-runtime.sh, in `core.nevia`'s commands there, and for both agents' AI; your machine's
  CLI still offers a newer release.
- `Computers.sleep` runs `nevia suspend` (`sleep` is only its alias).
- m2p's AI sessions run their commands in bash (`SHELL=/bin/bash`), as acc's: the agent computer's login
  shell may be zsh, where `echo == x` failed (run 261030153724).
- `agent m2p setup` replaces the source MongoDB (`mongo.computer`) with a fresh branch of a seed computer,
  `<mongo.computer>-seed`, and refuses while the agent runs: every setup gives the same data, as a fresh seed's.
  The seed computer is seeded again (made from scratch) only when its `~/seed.json` (`[m2p.seed]`, the
  database, `[mongodb] image`, seed.py's sha256) differs, and sleeps between setups; `destroy` deletes it,
  `clean` doesn't. It seeded only an empty source before.
- m2p's try branches are `<tries>N` exactly (`Settings.is_try`), no longer any name with that prefix; a
  `postgres.name` that would be one is refused.
- Both agents' `run` puts the agent computer to sleep once the run is over (`Local.asleep_after`, `nevia suspend`),
  however it ended, even when collecting fails; not after a Ctrl-C or while its job still runs; a sleep that
  fails says so. Every command wakes it again.
- m2p: mongo2pg's infer samples `[mongo2pg] sample_percent` of each collection (1 by default), not `init`'s
  1000 documents: the guide's commands set `percent` in each try's config.
- A cleanup and refactoring, with no change to the commands:
  - what both agents repeated is in core: the settings' shared fields and `load`, `Bootstrap.of`, the local and
    agent-side bases (`core/local/frame.py`, `core/agent/frame.py`), one deletion (`core.doomed.delete`) and one
    list-ask-delete protocol for `clean` and `destroy` (m2p's `clean` now lists before asking, as acc's), the
    tools and MongoDB setup, URIs into secrets, run ids and runs dirs, the AI session's result and its cost
    (`core/results.py`);
  - acc's run names are made and matched in one place (`RunNames`), m2p's branch prefix comes from the settings;
  - the agent computer reads its own files in Python: m2p's `status` without its shell script, and with
    `core.activity.events` (continuation lines kept), its loop job in your time zone as acc's are;
  - jobs run in a session of their own (`setsid`, `name.pid`) and `stop` stops it (`Computer.stop_job`), not
    `pkill` patterns; following rides out a short Nevia blip; m2p's `remote running` is gone;
  - `acc/agent.py` split into `acc/run.py` (both sides), `acc/cleanup.py` and `acc/stack.py`, its steps
    dispatched by a dict; m2p's follow printer and results in `m2p/follow.py` and `m2p/results.py`; your
    machine's modules import nothing of the agent computer's, and `tests/test_layers.py` checks every module's
    side;
  - tests share `tests/conftest.py` and `fake_nevia.job_ends`; core's tests moved up from `tests/m2p/`;
  - removed what nothing used (exception attributes, `Repo.name`, re-exports, never-passed parameters, the m2p
    agent computer's Python drivers), and fixed stale comments and docs.
- `docs/presentation.pptx`: text-only slides spread their bullets over the slide's height, in the largest size
  up to 20 pt that fits (`make_pptx.py`'s `spread_into`), as the Marp deck's `space-evenly`.
- agent.toml: GitHub Copilot CLI 1.0.105 (and its npm integrity), Maven 3.9.16 for acc's app.
- acc's `FEATURE.md`: the feature check's requests are built from the app's validation and its tests' examples,
  and each read against the code that handles it, since the AI can't run the check; a check that relies on an
  earlier request checks its status first (run 261030122635's check created a customer without its required
  email, and failed on a 400).
- Both agents' runs name the AI's files alike (`core.agent.ai`): `logs/ai-N.log`, `ai-N.jsonl`, `ai-N.json`
  per session (acc's `logs/ai.log` and `ai.jsonl` are `ai-1.*`, m2p's `logs/session-N.*` are `ai-N.*`), and
  `result.json` (`ai.total`, the sessions summed) and `report.md` at the run's root; acc's report's AI part is
  `ai.report`, which m2p's uses too.

- Each agent has its own Nevia workspace, `[m2p.nevia]` and `[acc.nevia]` (`m2p`, `acc`), instead of a shared
  `[nevia]`; `setup` makes it if need be. An agent's editor key, and acc's AI's, reaches its own only.
- `docs/presentation.pptx`, the deck in Aiven's brand template, made from `docs/presentation.md` by
  `docs/make_pptx.py`: native diagrams, and the two step-by-step animations on one slide each, a step per click.
- The prompts are in `prompts/` at the project's root, outside `src/`, and pymongo is a dev dependency only
  (seed.py runs on the source computer, with Debian's).
- Both agents drive Nevia with the `nevia` CLI (`core.nevia`), on your machine (`nevia login`) and on their
  computers (`[runtime] nevia`, installed by the runtime's setup and signed in with the agent's key), instead of
  nevia-kit, which only does the tunnels now. acc's AI gets the CLI too, with a copy of the key, to look at
  Nevia. `[nevia] cli_version` is now `[runtime] nevia`, `0.1.13`.
- One AI engine for both agents, `[ai] engine`: Claude Code (the default, `claude-sonnet-5-5`) or GitHub
  Copilot CLI, through the Aiven AI gateway (`core.agent.ai`: install, env, command, render, result); m2p
  used to run Copilot only, acc Claude only. acc's step `claude` is now `ai`, its Unix user `acc-ai`, its log
  `logs/ai.log`; both renders are `core/scripts/render-claude.jq` and `render-copilot.jq`.
- `agent.toml` holds only what you may choose: `[ai]` and `[mongodb]` (image, cache) are shared, and secret
  names, `key_path`, MongoDB's and chisel's users and Claude's user are conventions in the code.
  `gateway_models` is gone.

### Removed

- m2p's Copilot cost (`usage.py`, `price_*`, the token table at the end of `run`).

### Fixed

- acc's stack step failed after a first run (run 261030155307: `nevia rm acc-app exited 5: Checkpoint ... is in
  use by 1 live computer`): Nevia keeps a computer while one restored from its checkpoint lives, and each run
  leaves its sandbox. `Stack.deploy` now deletes the earlier runs' sandboxes, then their MongoDB forks
  (`RunNames.computers`, the current run's excluded), each logged, before `acc-app` and `acc-mongo`; the
  previous preview URL goes away when a new run starts its stack. The fake `nevia rm` refuses as Nevia does.

- One project for both agents, nevia-agent: m2p (from nevia-mongo-to-pg) and acc (this repository's uc2,
  rewritten), under one CLI, `agent m2p ...` and `agent acc ...`, with one `agent.toml` (`[nevia]` and
  `[mongo2pg]` shared, then `[m2p.*]` and `[acc.*]`) and shared code in `core`: settings, the
  local shell, mongo2pg's release binary, computers and their jobs.
- The secrets both agents need come from `.env` (`.env.example` lists them), loaded at each command; the
  shell's variables win.
- acc's Nevia names are `acc-app`, `acc-mongo`, `acc-pg`, apart from m2p's `hello-*`: they share a workspace.
- Collected runs go to `build/m2p-runs/` and `build/acc-runs/`.
- Every Nevia name carries its agent: resources `m2p-...` and `acc-...` (checked when `agent.toml` is read;
  m2p's were `hello-mongo`, `hello-pg`, `hello-agent`), workspace secrets `M2P_...` and `ACC_...` (m2p's were
  `HELLO_PG_URL`, `AIVEN_AI_API_KEY`, `NEVIA_AGENT_KEY`). acc's `secret_prefix` setting is gone.
- Your machine only makes the agent computer, its key and the secrets, sends it this code (uv, Python and the
  dependencies there, nevia-kit as source) and follows it; the agent does the rest from its computer through
  `agent <m2p|acc> remote ...`. acc's agent has a key again; Claude runs as its own Unix user, `acc-claude`.
- acc's `setup` makes no stack: each run recreates it.
- m2p keeps every `m2p-tryN` branch until `agent m2p clean`: the agent loop has no `drop.sh` any more, and its guide
  forbids deleting a branch.
- `agent acc run` always takes a mode: `migrate`, `task` or `resume`; `resume --from STEP` takes the last run up
  from that step, without the ones before (e.g. Claude).
- acc's `app-up.sh` stops whatever runs the app (a restored checkpoint brings it back running) and waits for
  the port before it starts the new jar: a sandbox no longer goes on serving the old app.
- The agents' logs read in your machine's time zone.
- `setup` and `run` show every line the agent's job writes, the seed's detail included, not only the last one
  at each poll (`Computer.follow_job`).
- What both agents run on their computers is shared in `core/`: `lib.sh` (on every computer as
  `~/.nevia-agent/lib.sh`, with `apt_install` and `fetch_checked` for every checked download), one MongoDB
  setup (`mongo-host.sh`, `mongo-up.sh`: the `[images] mongo` image in a Podman pod; for acc, with auth, its
  seed data through the image's init scripts, and chisel, instead of its own MongoDB binaries), and
  `common.jq`, so that both agents' logs read alike (`$ command`, `edit`, `says:`, `failed:`), parsed by
  `core.activity`.
- A download that an exec answers without a result is read again (`core.remote.Computer.download`).
- `src/` holds the packages themselves, `core`, `m2p`, `acc` and `prompts`, with no `nevia_agent` level; the
  `agent` CLI is `core.cli`.
- The code is laid out by where it runs: `core/local/` and each agent's `local.py` on your machine,
  `core/agent/` and each agent's `agent.py` (with the hidden `remote` commands) on the agent computer, the
  scripts in `core/scripts/` and `<agent>/scripts/`; `tests/test_layers.py` checks that the agent's side
  never imports your machine's.
- Both agents collect a run the same way (`core.local.collect`): one archive of the run's directories into
  `build/<agent>-runs/<run>/`, with the job's log, `skipped.txt` and URIs scrubbed; m2p's runs get an id as
  acc's, and acc's results now hold Claude's logs, the analysis, the feature check and `change.patch`.
- acc's prompts have a `## Commands` section, as m2p's guide has "Running mongo2pg": the build, the seed into
  its Postgres, the app started and stopped, the feature check's syntax. Its scripts share `git_token` and
  `db_kind` from `lib.sh` instead of each their own.
- The agent computer's work is Python (`m2p/steps.py`, `acc/steps.py`), not shell: 9 scripts gone
  (`run-agent.sh` and `setup-agent.sh` for m2p; `clone`, `analyse`, `run-claude`, `build`, `pg-init`, `pr`,
  `setup-agent` for acc). A step runs there as a subprocess, or as a job (`agent <agent> remote step ...`)
  when it needs a secret; `Computer` runs commands and writes files locally on its own computer. Shell stays
  where Python can't be: the MongoDB and app computers, the runtime's setup, the AI agents' tools.
- `docs/presentation.md`, 10 slides: the migration's path, and Nevia's concepts (computers, databases,
  branches, checkpoints, secrets and keys) and why they help.
- Less code: no `Computer.job_started` or `Computer.place` (`Builder.install` puts mongo2pg in place for both
  agents), no `runtime.command` (`agent_command`), no `script()`/`loop_file()` (`Computer.put`), no
  `pr.Validation.deploy_log`; `usage` reads the rendered `usage:` lines only; m2p's facade is `Local`, as acc's;
  acc's files on its stack's computers are under `~/.nevia-agent`, as the rest.
- One way to run a local command (`core.shell.run`), for `Computer.sh` here, the agents' steps and mongo2pg's
  download (`Builder.fetch`); one `core.doomed.Doomed` for what `clean` and `destroy` delete, sent as JSON
  from the agent computer and shown the same way for both agents.
- acc's pull request follows GitHub's redirects, finds the one already open for its branch when a run is
  taken up, and fails with GitHub's message otherwise.
- Every prompt is a file in `src/prompts/<agent>/`, one per task, all in the shape of m2p's guide: m2p's
  `AGENTS.md`, which is now each Copilot session's prompt too (the session prompt was inline in
  `run-agent.sh`), and acc's `MIGRATE.md` and `FEATURE.md`, rewritten in that shape (what is here, how to
  check, how to work, the reply as a template, rules: Write and Edit for files, one command per Bash call).
  The data's description is `m2p.usecase.DESCRIPTION`.
- One README for both, with the migration as a whole and each agent's architecture and sequence;
  `docs/m2p.md` and `docs/acc.md` for the detail.

### m2p

#### Added

- The agent loop on Nevia: Copilot CLI on its own computer, with a model from the Aiven AI gateway, works
  out how to migrate the hello MongoDB (nevia-helloworld's customers, as agent/uc3 seeds them) to
  PostgreSQL with the release binary of mongo2pg (`mongo2pg.release`, checked against its sha256). It
  tries its loads on throwaway branches of the target and loads the target once a try passes. To fix a
  failure it fixes the data first (with mongo2pg's configuration, or in one SQL file run after the
  import, never in MongoDB),
  and changes the SQL model only when that can't pass; never mongo2pg's code, and no JSONB or JSON.
- A load passes when mongo2pg's post-import report (`mongo2pg report --post-import`) has the same row
  count and matching MD5 for every table (on the count alone for a table `normalize.sql` converted),
  and the agent checks it no other way. Each load has its own mongo2pg project, its report included.
- `agent m2p setup` creates or finishes the source computer (MongoDB, with the data `seed.py` generates
  there), the target Postgres, the secrets, the agent computer and its link to MongoDB.
- `agent m2p run` sends the guide and tools, starts the agent and follows it (its intents and errors and
  a one-line status, or every action with `--details`). When the agent stops, it copies the run's
  results to `build/m2p-runs/`, with connection URIs scrubbed, and prints the tokens used and their
  cost at `m2p.copilot.price_*`. Run again while the agent runs, it follows that run.
- `run` prints a timeline in local time: sessions, branches, each try's result from `ATTEMPTS.md` and
  what changes after a failed one, what the agent says and what fails. `DONE.md` follows a short
  template: the result, what changed and why, and one line per try.
- The collected results have `final.sql`, all the SQL the target was loaded with, in order: the DDL the
  final import applied (from its log), then `normalize.sql` if any. The agent applies no other SQL to
  the target; `DONE.md` points to both files.
- `agent m2p status` shows the agent's state, tries and latest actions; `agent m2p stop` stops it, and
  a `run` following it then collects the results.
- `agent m2p clean` removes the last run's work for a fresh run: the agent's files, the `m2p-try*`
  branches and the target's schema. It asks first.
- `agent m2p destroy` deletes everything `setup` made on Nevia (computers, the target and its branches,
  the secrets, the agent's principal), after listing it and asking.
- One readable log per Copilot session, `logs/session-N.log`: `tools/render.jq` turns Copilot's JSON
  events into one line each as they come, and the end line has Copilot's exit code.
- `seed.py`, a port of uc3's `seed.mjs`: the same `[m2p.seed]` settings give the same documents. A share of
  the customers can have anomalies (`strange`, `strange_kinds`, a list of kinds); `[]` gives only
  well-formed data.

- `m2p.toml` is grouped by resource: `[nevia]`, `[mongo]` (the source, its database also naming the
  Postgres database and schema), `[m2p.seed]`, `[postgres]`, `[mongo2pg]`, `[agent]` and `[copilot]`, whose
  `reasoning_effort` sets Copilot's `--reasoning-effort`.

#### Removed

- The earlier dataset and everything around it, the local MongoDB stack and its snapshots, the replay
  of known fixes (`m2p migrate`, `patches/mongo2pg`), demos, builds of mongo2pg, the agent's own verifier,
  the `justfile` and `docs/`: the project is the agent loop on Nevia and nothing else.

### acc

#### Changed

- Rewritten in Python on the stack of nevia-mongo-to-pg: uv, Typer and Rich, `acc.toml` validated by
  pydantic instead of `.env`, nevia-kit instead of the `nevia` CLI, ruff, ty, pytest with `FakeApi`, prek.
- One CLI, `agent acc`: `setup`, `run migrate|task|resume`, `status`, `stop`, `clean`, `destroy`. `run resume` takes the
  last run up from the step that did not finish (was `resume`); each run deploys the stack (was `deploy`);
  the report lands in `build/acc-runs/<run>/` (was `report`); `clean` and `destroy` replace `ls`, `cleanup` and
  `purge`.
- This machine drives a run; the agent computer has no Nevia key, and the GitHub token and the gateway
  key reach it per command only.
- Claude Code goes through the Aiven AI gateway with nevia-mongo-to-pg's settings: `acc.claude.gateway_url`,
  `acc.claude.gateway_secret` (`AIVEN_AI_API_KEY`, required by `setup`), `model`, `reasoning_effort`; an empty
  `gateway_url` keeps a Claude Code login on the agent computer.
- Every workspace secret acc makes is named after `acc.secret_prefix` (`ACC_` by default).
- The mongo2pg analysis is made on Nevia, on the stack's MongoDB, instead of on your machine.

#### Removed

- uc1, uc3, `computer.sh` and `common/`: the shared computer, its key rotation and its register of runs.
