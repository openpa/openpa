/**
 * Shared types for the update surfaces (UpdateBanner + UpdatesSettings).
 *
 * ``BackendStatus`` is the response shape returned by
 * ``GET /api/upgrade/check`` — see ``app/api/upgrade.py``. The Python
 * endpoint emits one of a small set of ``status`` values; this discriminated
 * union captures the payload fields that come with each.
 *
 * ``UpdaterStatus`` is the desktop-app side, sourced from electron-updater
 * via the ``window.openpa.updater.onStatus`` bridge. We just re-export the
 * global type declared in ``vite-env.d.ts`` so consumers can import both
 * types from one place.
 */

export type BackendStatus =
  | { status: 'unknown' }
  | { status: 'up_to_date'; current: string; latest?: string; channel?: string; release_url?: string }
  | {
      status: 'available'
      current: string
      latest: string
      channel?: string
      release_url: string
      release_notes: string
      apply_command: string
      min_compatible_ui?: string
    }
  | {
      status: 'too_old'
      current: string
      latest: string
      channel?: string
      release_url: string
      min_supported_upgrade_from: string
    }
  | { status: 'unreachable'; current?: string; channel?: string; reason: string }
  | { status: 'unavailable'; current?: string; reason: string }
  | { status: 'error'; reason: string }

export type UpdaterStatus = OpenPAUpdaterStatus

// Re-exports for the in-app backend-upgrade flow so consumers can
// import all upgrade-related types from one module instead of dipping
// into the global vite-env declarations directly.
export type BackendUpgradePhase = OpenPABackendUpgradePhase
export type BackendUpgradeStatus = OpenPABackendUpgradeStatus
export type BackendUpgradeLog = OpenPABackendUpgradeLog
export type BackendUpgradeDone = OpenPABackendUpgradeDone
