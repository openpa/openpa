"""Single source of truth for the OpenPA version.

Read by ``pyproject.toml`` (via ``tool.hatch.version``) and by the runtime
``/version`` endpoint, the ``openpa version`` CLI, and the upgrader. Bump this
on release and the package metadata, the API response, and the CLI output
all move together.

Schema migrations are tracked separately by Alembic — see ``alembic/`` at the
repo root. ``MIN_SUPPORTED_UPGRADE_FROM`` is the oldest version this build
knows how to migrate from; the upgrader refuses to proceed when the live
install is older than this.

NOTE on ``"0.0.0"``: the project shipped ``v1.0.0`` as a final release
before any pre-release tags existed, which means every subsequent
``v1.0.0rcN.devM`` sorts *below* ``"1.0.0"`` under PEP 440. Keeping the
floor at ``"1.0.0"`` caused the upgrader to refuse every RC-to-RC upgrade
with "OpenPA is too old to upgrade in place" — see
``runner.check()`` line 134. ``"0.0.0"`` accepts any install; bump it
the next time Alembic introduces a migration that genuinely requires
a minimum schema version, not before.
"""

__version__ = "1.0.0"
MIN_SUPPORTED_UPGRADE_FROM = "0.0.0"
