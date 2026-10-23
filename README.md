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
        agent["hello-agent<br/>Claude Code + deploy/"]
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
    participant agent as hello-agent
    participant gh as GitHub
    participant base as hello-app + MongoDB
    participant sb as Sandbox

    dev->>m2p: mongo2pg infer on the local MongoDB
    m2p-->>dev: reports in migration/: schemas, stats, infer warnings, SQL, HTML
    dev->>agent: agent.sh migrate: scripts + reports (schemas, stats, SQL only, no sample values)
    agent->>gh: clones main
    agent->>base: recreates the app and MongoDB (seed data: local/mongo-init/*.js), checks them
    Note over agent: Claude reads the reports (../analysis, read-only)<br/>moves the code to Postgres, answers each infer warning<br/>turns local/mongo-init/*.js into local/postgres-init/*.sql<br/>Maven build (no access to Nevia or GitHub)
    agent->>sb: checkpoint of the app + new Postgres loaded with local/postgres-init (psql), deploys the new code, checks it
    agent->>gh: pushes agent/RUN, opens the PR (migration, mongo2pg analysis table, tests, preview)
    agent-->>dev: PR link
    Note over agent: goes to sleep 20 s later
```

A step that fails stops the run, with no PR. The HTML reports and the data stay on your machine.

## Setup (once)

Prerequisites: `nevia` (then `nevia login`), `jq`, `openssl`, and a GitHub fine-grained token on the app's repo (*Contents* and *Pull requests*: read and write).

```sh
cp .env.example .env          # set NEVIA_WORKSPACE, GITHUB_TOKEN, REPO
./agent.sh setup              # agent computer, its Nevia key, the token as a secret
nevia shell hello-agent       # then: claude, /login
```

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
./agent.sh migrate                       # runs the migration of REPO, prints the PR link
./agent.sh logs [<run>]                  # follows a run, Claude's steps included
./agent.sh stop [<run>]                  # stops a run (default: the one going)
./agent.sh ls                            # sandboxes
./agent.sh preview <run>                 # wakes a PR's sandbox, prints its URL
./agent.sh cleanup <run>                 # deletes a sandbox
./agent.sh purge [--yes]                 # deletes every run and the app with its databases (all data)
./agent.sh destroy [--yes]               # deletes the agent
```

The prompt is `tasks/mongo-to-postgres.txt`. All settings are in `.env` only (see `.env.example`), never in the shell or as arguments.

## Code

```
agent.sh                   on your machine: setup, migrate, follows the run
agent-bootstrap.sh         on the agent: the run, ls, preview, cleanup
tasks/mongo-to-postgres.txt   Claude's task
deploy/deploy.sh           the deploy layer: deploy, recreate, check, down, destroy (SANDBOX=<run> for a sandbox)
deploy/app/bootstrap.sh    on the app computer: JDK, Maven, build, start
deploy/mongo/bootstrap.sh  on the MongoDB computer: MongoDB, tunnel, seed data (local/mongo-init)
```

## Security

- Claude has neither the GitHub token nor the Nevia key, only file tools, Maven, and podman + psql to try its SQL on a throwaway PostgreSQL.
- It sees the structure of the data (fields, types, counts), never the values.
- The agent's Nevia key is *editor* on the workspace: use a dedicated workspace to isolate it.
