# acc: the code

Claude Code, on its own Nevia computer, changes an app and proposes the change as a pull request once a sandbox
has passed: its migration from MongoDB to Postgres (`run migrate`), or a feature you describe
(`run task "..."`, or `-f file`), e.g. for [nevia-helloworld](https://github.com/bertrandcedric/nevia-helloworld).
It never pushes to the base branch and never merges. Setup, commands and naming: [README](../README.md).

## Resources

| Name | What |
| --- | --- |
| `acc-app`, `acc-mongo` | the app's stack, made by each run from its clone of the base branch (`setup` makes none); MongoDB runs as m2p's does (the same image, in a Podman pod), with auth and the seed data, and the app reaches it through chisel |
| `acc-agent` | runs the whole run, with its key (`ACC_NEVIA_AGENT_KEY`); Claude Code, mongo2pg's binary, JDK, Maven, psql |
| `acc-claude` | the Unix user Claude runs as on `acc-agent`: it can't read the key file nor the orchestrator's env |
| `acc-app-RUN` | a run's sandbox: a checkpoint (`acc-RUN`) of `acc-app` |
| `acc-pg-RUN` or `acc-mongo-RUN` | its database: a new Postgres when the change moves to it, a branch of `acc-mongo` (a fork of its data) otherwise |
| `acc-app-db-RUN` | Claude's own empty Postgres for a migration, deleted once it is done |

The GitHub token (`ACC_GITHUB_TOKEN`) reaches the agent only to clone and push, the gateway key
(`ACC_AIVEN_AI_API_KEY`) only for Claude. Claude sees the structure of the data (fields, types, counts), never
its values. Settings: `[acc.*]`, with the gateway as m2p's (`gateway_url`, `gateway_secret`, `model`,
`reasoning_effort`); an empty `gateway_url` means a Claude Code login on `acc-agent` instead.

```mermaid
flowchart LR
    cli["agent acc<br/>(your machine)"]
    github[("GitHub")]
    gateway["Aiven AI gateway"]
    subgraph nevia["Nevia"]
        agent["acc-agent<br/>Claude Code, mongo2pg,<br/>JDK, Maven, psql"]
        app["acc-app"] -- "chisel" --> mongo[("acc-mongo")]
        sbapp["acc-app-RUN"] --- sbdb[("acc-pg-RUN or<br/>acc-mongo-RUN")]
        dev[("acc-app-db-RUN")]
    end
    cli -. "starts a run, follows it" .-> agent
    agent -- "databases, checkpoint,<br/>restore, deploy" --> app
    agent --> sbapp
    agent --> dev
    agent -- "link, for the analysis" --> mongo
    agent -. "Claude's checks" .-> dev
    agent -- "clone, push, PR" --> github
    agent -- "model calls" --> gateway
```

## A run

```mermaid
sequenceDiagram
    autonumber
    actor you as You
    participant cli as agent acc
    participant agent as acc-agent (Claude Code)
    participant gh as GitHub
    participant stack as acc-app + acc-mongo
    participant sb as Sandbox

    you->>cli: run migrate (or run task "...")
    cli->>agent: starts the run, then follows its log
    agent->>gh: clone the base branch, dry-run push of agent/RUN
    agent->>stack: recreate from the clone, check /healthz and /
    agent->>stack: mongo2pg analysis of acc-mongo (migrate only)
    Note over agent: Claude Code, as acc-claude: changes the code and its tests,<br/>checks once against its own Postgres (migrate)
    agent->>agent: mvn package
    agent->>sb: checkpoint of acc-app restored, its database, the jar, check /healthz and /
    agent->>sb: the feature check Claude wrote (task only)
    agent->>gh: push agent/RUN, pull request
    cli->>you: the pull request, and build/acc-runs/RUN/ (report, PR text, state)
```

- **clone**: the base branch, and a dry-run push of `agent/RUN` to check the token first.
- **stack**: `acc-app` and `acc-mongo` recreated from the clone (seed data `local/mongo-init/*.js`), never an
  older version.
- **analysis** (migrate): mongo2pg `init` and `infer` on `acc-agent`, through a nevia-kit link to `acc-mongo`;
  Claude gets the schemas, stats, warnings, mappings and SQL read-only, without the sample values.
- **claude**: the task is `src/prompts/acc/MIGRATE.md`, or `FEATURE.md` around your request. For a
  task, Claude also writes a feature check: a script that calls the new endpoints of a deployed app.
- **build**: `mvn package`, the tests included.
- **sandbox**: `acc-app-RUN`, on `acc-pg-RUN` loaded with `local/postgres-init/*.sql`, or on a fork of
  `acc-mongo`; the jar built on the agent must answer.
- **feature check** (task): Claude's script against the sandbox, with nothing but `PATH` and `HOME`.
- **pr**: Claude's text, the analysis, the validation, the feature check, the preview URL.

A step that fails stops the run with no pull request. Each step is recorded once done in the run's
`state.json` on `acc-agent`, so `run resume` takes the last run up from the step that did not finish, and
`run resume --from STEP` from an earlier one (e.g. `--from sandbox`: the sandbox again, not Claude); `run`
brings the state, the report and the pull request text back to `build/acc-runs/RUN/`.
`build/acc-runs/RUN/report.md` has the outcome, the time of each step, Claude's cost (Claude Code's estimate at
list prices) and what the run left on Nevia; `clean --run RUN` deletes that.
