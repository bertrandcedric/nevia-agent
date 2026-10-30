"""`agent`: agents on Nevia. `agent m2p` works out a MongoDB to PostgreSQL migration of the data with mongo2pg;
`agent acc` changes an app's code (its migration to Postgres, or a feature) and opens a pull request."""

import os
from typing import Annotated

import typer

from acc import agent as acc_agent
from acc import local as acc
from core.config import WORKSPACE_ENV
from core.local import env
from core.nevia import NeviaError
from core.shell import CommandError, out
from m2p import agent as m2p_agent
from m2p import local as m2p

app = typer.Typer(no_args_is_help=True, help=__doc__, pretty_exceptions_show_locals=False)
# The one place both sides meet: each agent's commands (its `local`), and the hidden `remote` ones they run on the
# agent computer (its `agent`), where this same CLI runs.
for _local, _agent in ((m2p, m2p_agent), (acc, acc_agent)):
    _local.app.add_typer(_agent.remote, name="remote")


@app.callback()
def _options(
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", "-w", envvar=WORKSPACE_ENV, show_envvar=True,
                     help="The Nevia workspace, instead of agent.toml's `[<agent>.nevia] workspace`."),
    ] = None,
) -> None:  # fmt: skip
    if workspace:
        os.environ[WORKSPACE_ENV] = workspace  # read by the settings here, passed on to the agent computer


app.add_typer(m2p.app, name="m2p")
app.add_typer(acc.app, name="acc")


def main() -> None:
    """Entry point: `.env` first, then the command. Failures of the tools the agents drive print as one message,
    not a traceback: Nevia's, a command's (CommandError, JobFailed), and the errors the code raises on purpose,
    RuntimeError, ValueError (agent.toml's, .env's, a request refused), OSError and TimeoutError. Any other, a
    bug, keeps its traceback."""
    env.load()
    try:
        app()
    except (NeviaError, CommandError, RuntimeError, ValueError, OSError, TimeoutError) as err:
        out.print(f"[red]error:[/] {err}")
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        out.print("\nstopped following: the agent keeps going on Nevia; `run` follows it again, `stop` stops it")
        raise SystemExit(130) from None
