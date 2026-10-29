"""`agent`: agents on Nevia. `agent m2p` works out a MongoDB to PostgreSQL migration of the data with mongo2pg;
`agent acc` changes an app's code (its migration to Postgres, or a feature) and opens a pull request."""

import typer

from acc import local as acc
from core.local import env
from core.nevia import NeviaError
from core.shell import CommandError, out
from m2p import local as m2p

app = typer.Typer(no_args_is_help=True, help=__doc__, pretty_exceptions_show_locals=False)
app.add_typer(m2p.app, name="m2p")
app.add_typer(acc.app, name="acc")


def main() -> None:
    """Entry point: `.env` first, then the command. Failures of the tools the agents drive print as one message,
    not a traceback."""
    env.load()
    try:
        app()
    except (NeviaError, CommandError, RuntimeError, ValueError, OSError, TimeoutError, KeyError) as err:
        out.print(f"[red]error:[/] {err}")
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        out.print("\nstopped following: the agent keeps going on Nevia; `run` follows it again, `stop` stops it")
        raise SystemExit(130) from None
