"""`huskd` must stay a single-command Typer app.

Typer makes the subcommand OPTIONAL when an app has exactly one command and
MANDATORY as soon as it has two. The container runs `ENTRYPOINT ["huskd"]` with
`CMD ["--config", "/etc/husk/config.toml"]`, so adding a second `huskd` command
turns every container start into `Missing command.` — a break that is invisible
from Python and only shows up at deploy time.
"""

from __future__ import annotations

from typer.testing import CliRunner

from husk.cli import huskd_app

runner = CliRunner()


def test_huskd_takes_options_with_no_subcommand():
    # Mirrors the Dockerfile's ENTRYPOINT + CMD exactly. A nonexistent config makes
    # this exit 2 on the config check — which is proof it got PAST argument parsing
    # into `run`. A second huskd command would fail at parsing instead.
    result = runner.invoke(huskd_app, ["--config", "/nonexistent/husk.toml"])
    assert "Missing command" not in result.output
    assert "config file not found" in result.output
    assert result.exit_code == 2
