"""`opa serve` — boot the OpenPA HTTP server in-process.

This is the only CLI command that imports from `app.server`. The import is
performed lazily inside the function body so that `pip install openpa` (without
the `[server]` extra) does not need to satisfy server-only dependencies for
`opa --help` or any other subcommand to work.
"""

from __future__ import annotations

import typer


def serve(
    host: str = typer.Option(
        None,
        "--host",
        "-H",
        envvar="HOST",
        help="Bind address. Defaults to BaseConfig.HOST.",
    ),
    port: int = typer.Option(
        None,
        "--port",
        "-p",
        envvar="PORT",
        help="Bind port. Defaults to BaseConfig.PORT.",
    ),
) -> None:
    """Start the OpenPA HTTP server in-process."""
    import asyncio

    try:
        from app.server import main as server_main, DEFAULT_HOST, DEFAULT_PORT
    except ImportError as e:
        typer.echo(
            "`opa serve` requires the server extra.\n"
            "Install with:  pip install 'openpa[server]'",
            err=True,
        )
        raise typer.Exit(code=1) from e

    asyncio.run(
        server_main(
            host=host if host is not None else DEFAULT_HOST,
            port=port if port is not None else DEFAULT_PORT,
        )
    )
