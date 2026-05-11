"""Single source of truth for the OpenPA version.

Read by ``pyproject.toml`` (via ``tool.hatch.version``) and by the runtime
``/version`` endpoint, the ``openpa version`` CLI, and the upgrader. Bump this
on release and the package metadata, the API response, and the CLI output
all move together.

Schema migrations are tracked separately by Alembic — see ``alembic/`` at the
repo root. ``MIN_SUPPORTED_UPGRADE_FROM`` is the oldest version this build
knows how to migrate from; the upgrader refuses to proceed when the live
install is older than this.

``MIN_COMPATIBLE_UI`` is the oldest UI version that talks correctly to this
backend. The UI consults the ``/version`` endpoint at startup and shows an
upgrade-required banner if it falls below this floor.
"""

__version__ = "0.1.7"
MIN_SUPPORTED_UPGRADE_FROM = "0.1.0"
MIN_COMPATIBLE_UI = "0.1.0"
