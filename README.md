# Claude Code agents on Nevia

Three use cases, two Nevia computers (uc1 and uc2 share one, uc3 has its own):

| | What | Script |
| --- | --- | --- |
| [uc1](uc1/README.md) | a MongoDB (Rocket.Chat) to Nevia Postgres, with mongo2pg | `uc1/rc-lab.sh` |
| [uc2](uc2/README.md) | an app's code changed as a pull request: its migration to Postgres (`migrate`), or a feature you describe (`task`), tested on a sandbox (checkpoint of the app, fork of its MongoDB) | `uc2/agent.sh` |
| [uc3](uc3/README.md) | nevia-helloworld's data (generated, with strange documents) to Nevia Postgres | `uc3/lab.sh` |

One config for all three: `.env` (from `.env.example`). A plain `KEY` is for every use case, `UC1_KEY`, `UC2_KEY`, `UC3_KEY` for one only (it wins).

## The computer

`AGENT` (`nevia-agent` by default) is the computer of uc1 and uc2: the first `setup` of either makes it, and each one adds what it needs (uc1 the Rocket.Chat port, uc2 the GitHub token). uc3 has its own, `uc3-agent`, with its own principal and key (secret `UC3_NEVIA_KEY`), handled the same way (`./lab.sh rotate-key`). Each keeps its files apart on it: `~/m2p` (uc1), `~/agent` (uc2), `~/uc3` (uc3), and uc1's MongoDB listens on 27017, uc3's on 27018. Claude Code logs in once for all of them (`nevia shell nevia-agent`, then `claude` and `/login`), or uses the credential in `.env`.

```sh
./computer.sh setup               # the computer, its key (only if it has none), Claude Code's credential
./computer.sh status              # the computer, its key's expiry, the runs going on it
./computer.sh rotate-key          # a new key, the old one revoked: refused while a run is going
./computer.sh sleep               # to sleep, unless a run is going
./computer.sh destroy [--yes]     # the computer (every use case's files), its principal and key, Claude Code's secrets
```

Each use case's `destroy` deletes only what is its own (its files on the computer, its databases, its secrets).

## Two agents at once, and the Nevia key

Two agents may run on the computer at the same time (e.g. a uc1 migration and a uc2 one). The key is handled so that neither loses its access to Nevia midway:

- **One principal, one key**: the principal `AGENT` (*editor*), its key in the secret `NEVIA_AGENT_KEY`, in the computer's env as `AGENT_NEVIA_KEY`, with its expiry as `AGENT_NEVIA_KEY_EXPIRES`.
- **`setup` never renews it**: it makes a key only when there is none, so a `setup` during a run changes nothing that run uses. `./computer.sh rotate-key` renews it (`AGENT_KEY_TTL`, 30 days by default), and refuses while a run is going: a rotation revokes the old key.
- **One key file per process**: each script on the computer writes the key to a file of its own (this user only), which the scripts it starts share, and deletes it when it ends. uc2 deletes its file while Claude runs; uc1 and uc3 give Claude `nvpg` only, which uses its run's file, but that file is on the computer, readable by Claude.
- **A register of runs**: each run (and each step on the computer) is in `~/.busy/<uc>-<id>` with its pid while it goes. Nothing puts the computer to sleep, rotates the key or deletes the computer while one is there.
- **Expiry**: each run warns when the key expires within 3 days, and stops with `./computer.sh rotate-key` when it has expired or Nevia refuses it.

The key is *editor* on the workspace: use a dedicated workspace.
