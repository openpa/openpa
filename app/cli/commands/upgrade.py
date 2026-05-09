"""`opa upgrade` — full backup → install → migrate → health flow.

Wraps :mod:`app.upgrade.runner`. Two subcommands:

  upgrade check      Look up the latest release; report whether one is
                     available without making any changes.
  upgrade apply      Run the upgrade flow. Prompts for confirmation
                     unless ``--yes`` is passed.

Both require the ``[server]`` extra (pip, alembic, sqlalchemy, the
backup helper). The slim CLI install errors out gracefully.
"""

from __future__ import annotations

import typer


upgrade_app = typer.Typer(
    name="upgrade",
    help="Check for and apply OpenPA upgrades.",
    no_args_is_help=False,
    invoke_without_command=True,
)


def _require_server_extra():
    try:
        from app.upgrade import runner  # noqa: F401
        return runner
    except ImportError as e:
        typer.echo(
            "`opa upgrade` requires the server extra.\n"
            "Install with:  pip install 'openpa[server]'",
            err=True,
        )
        raise typer.Exit(code=1) from e


def _print_event(event) -> None:
    """Render an UpgradeEvent for the CLI in a single line."""
    prefix = "✓ " if event.ok else "✗ "
    typer.echo(f"{prefix}[{event.kind}] {event.message}")


@upgrade_app.callback()
def _root(ctx: typer.Context) -> None:
    """Default action: ``opa upgrade`` with no subcommand runs ``apply``.

    Mirrors what most package-manager-style CLIs do (``brew upgrade``,
    ``cargo install --upgrade``) so users don't have to remember the
    explicit subcommand for the common case.
    """
    if ctx.invoked_subcommand is not None:
        return
    ctx.invoke(upgrade_apply)


@upgrade_app.command("check")
def upgrade_check() -> None:
    """Check whether a newer version is available without changing anything."""
    runner = _require_server_extra()
    release, status = runner.check(callback=_print_event)
    if status == "available" and release is not None:
        typer.echo(f"\nRun `opa upgrade apply` to install {release.version}.")
    raise typer.Exit(code=0 if status in ("up_to_date", "available") else 1)


@upgrade_app.command("apply")
def upgrade_apply(
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the confirmation prompt.",
    ),
    target: str | None = typer.Option(
        None, "--target",
        help="Target version (must equal the latest release; for safety).",
    ),
) -> None:
    """Apply the upgrade. Stops on rollback if any step fails."""
    runner = _require_server_extra()

    def _confirm(release) -> bool:
        if yes:
            return True
        return typer.confirm(
            f"\nUpgrade to {release.version}? "
            f"This will take a backup, install the new package, and run migrations.",
            default=True,
        )

    success = runner.apply(target_version=target, callback=_print_event, confirm=_confirm)
    raise typer.Exit(code=0 if success else 1)
