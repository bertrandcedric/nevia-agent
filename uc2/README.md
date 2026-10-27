# Coding agent

A Claude Code agent on a Nevia computer that migrates an app from MongoDB to Postgres (e.g. [nevia-helloworld](https://github.com/bertrandcedric/nevia-helloworld)) and proposes the change as a pull request. It never pushes to `main` and never merges.

## Architecture

```mermaid
flowchart LR
    github[("GitHub")]

    subgraph local["Your machine"]
        direction TB
        cli["agent.sh"]
        m2p["migration/<br/>mongo2pg reports<br/>(schemas, stats, SQL, HTML)"]
    end

    subgraph nevia["Nevia"]
        direction LR
        agent["nevia-agent<br/>Claude Code + deploy/"]
        secrets[/"Secrets<br/>GITHUB_TOKEN<br/>NEVIA_AGENT_KEY"/]
        base["hello-app + hello-mongo"]
        subgraph sandbox["Sandbox of a run"]
            sbapp["hello-app-RUN<br/>(checkpoint)"]
            sbdb[("hello-pg-RUN<br/>(new Postgres,<br/>local/postgres-init)")]
        end
    end

    m2p -- "schemas, stats, SQL<br/>(no sample values)" --> cli
    cli -- "scripts + analysis" --> agent
    secrets -. "env" .-> agent
    agent -- "recreates, tests" --> base
    agent -- "creates, tests" --> sandbox
    agent -- "clone, PR" --> github
```

## Workflow of a migration

```mermaid
sequenceDiagram
    autonumber
    actor dev as You
    participant m2p as mongo2pg
    participant agent as nevia-agent
    participant gh as GitHub
    participant base as hello-app + MongoDB
    participant sb as Sandbox

    dev->>m2p: mongo2pg infer on the local MongoDB
    m2p-->>dev: reports in migration/: schemas, stats, infer warnings, SQL, HTML
    dev->>agent: agent.sh migrate: scripts + reports (schemas, stats, SQL only, no sample values)
    agent->>gh: clones main
    agent->>base: recreates the app and MongoDB (seed data: local/mongo-init/*.js), checks them
    Note over agent: Claude reads the reports (../analysis, read-only)<br/>moves the code to Postgres, answers each infer warning<br/>turns local/mongo-init/*.js into local/postgres-init/*.sql<br/>Maven build, one check on a Postgres of its own on Nevia<br/>(no Nevia key, no GitHub)
    agent->>sb: checkpoint of the app + new Postgres loaded with local/postgres-init (psql), deploys the new code, checks it
    agent->>gh: pushes agent/RUN, opens the PR (migration, mongo2pg analysis table, tests, preview)
    agent-->>dev: PR link
    Note over agent: goes to sleep 20 s later
```

A step that fails stops the run, with no PR. The HTML reports and the data stay on your machine.

## Setup (once)

The computer is shared with uc1 and uc3 (`AGENT`, `nevia-agent` by default): its Nevia key, its sleep and its deletion are `../computer.sh`'s (see [../README.md](../README.md)); two agents may run on it at once.

Prerequisites: `nevia` (then `nevia login`), `jq`, `openssl`, and a GitHub fine-grained token on the app's repo (*Contents* and *Pull requests*: read and write).

```sh
cp ../.env.example ../.env    # once for uc1, uc2, uc3: set NEVIA_WORKSPACE, UC2_GITHUB_TOKEN, UC2_REPO
./agent.sh setup              # agent computer, its Nevia key, the token as a secret (again: renews the key)
nevia shell nevia-agent       # then: claude, /login (not needed with a gateway, see below)
```

Claude Code through a gateway (e.g. Aiven's AI gateway) instead of a login: set `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_MODEL` (and the `ANTHROPIC_DEFAULT_*_MODEL` if the gateway names the models its own way) in `.env`, then `./agent.sh setup`. The token becomes a workspace secret in the agent's env; the URL and the models go with each run, so a change needs no setup.

## Data analysis (mongo2pg)

From this folder, with the app's local MongoDB and Postgres running (`docker compose --profile postgres up -d` in the app):

```sh
mongo2pg init --project-base "$PWD/migration" --project-name nevia-helloworld --namespace hello \
  --source-uri mongodb://127.0.0.1:27017 \
  --target-uri "postgres://postgres:postgres@127.0.0.1:5432/postgres?sslmode=disable"   # once
mongo2pg infer -c migration/nevia-helloworld/config/nevia-helloworld.toml
```

The project is named after the repo: `migrate` reads `migration/<repo name>`. `migration/` is not committed.

## Usage

```sh
./agent.sh deploy                        # deploys REPO only: the app and its MongoDB (data kept), checked; no Claude, no PR
./agent.sh migrate                       # runs the migration of REPO, prints the PR link
./agent.sh task "Add GET /customers/{id}/total, the sum of its orders"   # a feature, tested on a fork, as a PR
./agent.sh task -f feature.md            # the same, the request in a file
./agent.sh logs [<run>]                  # follows a run, Claude's steps included
./agent.sh report [<run>]                # fetches a run's report again (default: the latest)
./agent.sh stop [<run>]                  # stops a run (default: the one going)
./agent.sh resume <run>                  # redoes a failed run's sandbox and PR, on Claude's change (no new Claude)
./agent.sh ls                            # sandboxes
./agent.sh cleanup <run>                 # deletes a sandbox
./agent.sh purge [--yes]                 # deletes every run and the app with its databases (all data)
./agent.sh destroy [--yes]               # deletes the agent
```

At its end, failed or not, a run writes a report that `agent.sh` brings to `reports/<run>.md` (not committed): its outcome, PR and preview, the time of each step, Claude's cost (total, and tokens and cost per model: Claude Code's estimate at list prices; a gateway bills its own), and what the run left on Nevia. The Nevia CLI gives no prices: price the time at your plan's rates.

The prompt is `tasks/mongo-to-postgres.txt`. All settings are in `agent/.env` only, the one file of uc1, uc2 and uc3 (a `UC2_` line is uc2's alone and wins over the plain one; see `agent/.env.example`), never in the shell or as arguments.

## A task: a feature, tested on a fork of the data

`./agent.sh task "<what to build>"` asks Claude for a feature of REPO, in your words, wrapped in `tasks/feature.txt`. The run is the migration's, without the analysis and without Postgres:

1. clones REPO (`BASE_BRANCH`), recreates the app and its MongoDB (seed data: `local/mongo-init`), checks them;
2. Claude implements the feature with its unit tests, and writes a **feature check**: a `sh` script, outside the repository, that calls the new endpoints of a deployed app with curl and fails if they do not answer as they should;
3. `mvn package` (the tests run);
4. the sandbox: a checkpoint of the app restored as `hello-app-<run>`, on **`hello-mongo-<run>`, a branch of the MongoDB computer: a fork of its data**; the new jar deployed there, `/healthz` and `/` checked, then the feature check run against it (it may write: it is the fork's data). It runs without the GitHub token, the Nevia key or Claude's credential;
5. the PR: Claude's text, the validation, the feature check and its output, the preview URL.

A failed step: no PR. `./agent.sh resume <run>` redoes the sandbox, the check and the PR on Claude's change; `./agent.sh cleanup <run>` deletes the sandbox (app and fork).

## Code

```
agent.sh                   on your machine: setup, migrate, follows the run
agent-bootstrap.sh         on the agent: the run, ls, stop, cleanup, purge
tasks/mongo-to-postgres.txt   Claude's task
deploy/lib.sh              shared by all the scripts: nevia calls, pushing files, installing JDK, Maven, chisel
deploy/deploy.sh           the deploy layer: deploy, recreate, destroy (SANDBOX=<run> for a sandbox)
deploy/app/bootstrap.sh    on the app computer: JDK, Maven, build, start
deploy/mongo/bootstrap.sh  on the MongoDB computer: MongoDB, tunnel, seed data (local/mongo-init)
```

## Security

- Claude has neither the GitHub token nor the Nevia key. With a gateway, it has its token (`ANTHROPIC_AUTH_TOKEN`): Claude Code needs it, and the commands it runs inherit it. Give the lab a token of its own, with a spending limit. It may use file tools and a short list of commands without asking (Maven, Java, curl, psql, pg_dump, diff...; `CLAUDE_ALLOWED_TOOLS` in `.env` replaces the list), in the clone, the analysis and /tmp.
- To test its change, Claude gets an empty Postgres of its own on Nevia (`<app>-db-<run>`), through its environment only, deleted once it is done.
- It sees the structure of the data (fields, types, counts), never the values.
- The agent's Nevia key is *editor* on the workspace: use a dedicated workspace to isolate it.
