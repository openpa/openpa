/**
 * Cross-origin auth storage facade.
 *
 * In the Electron build, Chromium treats ``file://``, ``http://localhost``,
 * and ``http://127.0.0.1`` as three distinct origins, each with its own
 * localStorage. As the renderer navigates between these (asar →
 * wheel-served SPA → quit/relaunch), tokens written on one origin are
 * invisible to the next, and the user appears logged out.
 *
 * To dodge that, auth-critical data lives in a main-process JSON file
 * (``openpa-auth.json``) exposed through the preload bridge at
 * ``window.openpa.auth``. The facade in this file is the single point
 * of contact for the rest of the SPA: every settings-store call routes
 * through here, branching on ``__IS_ELECTRON__`` so the web build keeps
 * using localStorage exactly as before.
 */

type AuthSnapshot = {
  tokens: Record<string, string>
  loggedInProfiles: string[]
  activeProfileId: string
  reasoningEnabled: Record<string, boolean>
}

const EMPTY_SNAPSHOT: AuthSnapshot = {
  tokens: {},
  loggedInProfiles: [],
  activeProfileId: '',
  reasoningEnabled: {},
}

function isElectron(): boolean {
  return typeof __IS_ELECTRON__ !== 'undefined' && __IS_ELECTRON__
}

// Bridge access. The renderer mirrors writes back into this snapshot so
// later sync reads (e.g. ``getToken`` during a Pinia setup function)
// observe the latest value without a round-trip. Falls back to an empty
// shape when the bridge isn't there yet — happens briefly in dev when
// the preload hasn't injected before some test harness runs.
function bridgeSnapshot(): AuthSnapshot {
  const snap = window.openpa?.auth?.snapshot as Partial<AuthSnapshot> | undefined
  if (!snap) return EMPTY_SNAPSHOT
  return {
    tokens: snap.tokens ?? {},
    loggedInProfiles: snap.loggedInProfiles ?? [],
    activeProfileId: snap.activeProfileId ?? '',
    reasoningEnabled: snap.reasoningEnabled ?? {},
  }
}

async function applyPatch(patch: Partial<AuthSnapshot>): Promise<void> {
  const bridge = window.openpa?.auth
  if (!bridge) return
  // Optimistic update before the IPC round-trip. The renderer often
  // calls setToken(p, t) and immediately reads getToken(p) — for
  // example, ``setTokenForProfile`` followed by ``activateProfile``
  // inside SetupWizard.handleFinish. If we waited for ``bridge.set``
  // to resolve, the immediate read would still see the old snapshot.
  const snap = bridgeSnapshot()
  const optimistic: AuthSnapshot = {
    tokens: patch.tokens ? { ...snap.tokens, ...patch.tokens } : snap.tokens,
    loggedInProfiles: patch.loggedInProfiles ?? snap.loggedInProfiles,
    activeProfileId: patch.activeProfileId ?? snap.activeProfileId,
    reasoningEnabled: patch.reasoningEnabled
      ? { ...snap.reasoningEnabled, ...patch.reasoningEnabled }
      : snap.reasoningEnabled,
  }
  if (window.openpa?.auth) {
    window.openpa.auth.snapshot = optimistic
  }
  // Reconcile with the main-process result once the write resolves.
  // In practice ``next`` matches the optimistic state, but we trust
  // the main process as the source of truth in case validation
  // dropped something.
  const next = await bridge.set(patch)
  if (window.openpa?.auth) {
    window.openpa.auth.snapshot = next
  }
}

// ── Tokens ─────────────────────────────────────────────────────────────

export function getToken(profile: string): string {
  if (isElectron()) {
    return bridgeSnapshot().tokens[profile] ?? ''
  }
  return localStorage.getItem(`agent_token_${profile}`) || ''
}

export async function setToken(profile: string, token: string): Promise<void> {
  if (isElectron()) {
    const snap = bridgeSnapshot()
    const nextProfiles = snap.loggedInProfiles.includes(profile)
      ? snap.loggedInProfiles
      : [...snap.loggedInProfiles, profile]
    await applyPatch({
      tokens: { [profile]: token },
      loggedInProfiles: nextProfiles,
    })
    return
  }
  localStorage.setItem(`agent_token_${profile}`, token)
  const profiles = getLoggedInProfilesFromLocalStorage()
  if (!profiles.includes(profile)) {
    profiles.push(profile)
    saveLoggedInProfilesToLocalStorage(profiles)
  }
}

export async function removeToken(profile: string): Promise<void> {
  if (isElectron()) {
    const bridge = window.openpa?.auth
    if (!bridge) return
    // Optimistic update for the same reason as applyPatch — callers
    // may sync-read getLoggedInProfiles right after removeToken.
    const snap = bridgeSnapshot()
    const remainingTokens = { ...snap.tokens }
    delete remainingTokens[profile]
    const optimistic: AuthSnapshot = {
      ...snap,
      tokens: remainingTokens,
      loggedInProfiles: snap.loggedInProfiles.filter((p) => p !== profile),
    }
    if (window.openpa?.auth) {
      window.openpa.auth.snapshot = optimistic
    }
    const next = await bridge.removeToken(profile)
    if (window.openpa?.auth) {
      window.openpa.auth.snapshot = next
    }
    return
  }
  localStorage.removeItem(`agent_token_${profile}`)
  const profiles = getLoggedInProfilesFromLocalStorage().filter((p) => p !== profile)
  saveLoggedInProfilesToLocalStorage(profiles)
}

// ── Logged-in profiles ─────────────────────────────────────────────────

export function getLoggedInProfiles(): string[] {
  if (isElectron()) {
    return [...bridgeSnapshot().loggedInProfiles]
  }
  return getLoggedInProfilesFromLocalStorage()
}

function getLoggedInProfilesFromLocalStorage(): string[] {
  try {
    const raw = localStorage.getItem('logged_in_profiles')
    return raw ? JSON.parse(raw) : []
  } catch {
    return []
  }
}

function saveLoggedInProfilesToLocalStorage(profiles: string[]): void {
  localStorage.setItem('logged_in_profiles', JSON.stringify(profiles))
}

// ── Active profile ─────────────────────────────────────────────────────

export function getActiveProfileId(): string {
  if (isElectron()) {
    return bridgeSnapshot().activeProfileId
  }
  return localStorage.getItem('profile_id') || ''
}

export async function setActiveProfileId(id: string): Promise<void> {
  if (isElectron()) {
    await applyPatch({ activeProfileId: id })
    return
  }
  if (id) {
    localStorage.setItem('profile_id', id)
  } else {
    localStorage.removeItem('profile_id')
  }
}

// ── Reasoning toggle ───────────────────────────────────────────────────

export function getReasoningEnabled(profile: string): boolean {
  if (isElectron()) {
    const val = bridgeSnapshot().reasoningEnabled[profile]
    return val !== false
  }
  const raw = localStorage.getItem(`reasoning_enabled_${profile}`)
  return raw !== 'false'
}

export async function setReasoningEnabled(profile: string, enabled: boolean): Promise<void> {
  if (isElectron()) {
    await applyPatch({ reasoningEnabled: { [profile]: enabled } })
    return
  }
  localStorage.setItem(`reasoning_enabled_${profile}`, String(enabled))
}

// ── One-time migration: localStorage → bridge ──────────────────────────
//
// Called from settings.ts's module-init migration. Only acts when we're
// in Electron AND the bridge slot is empty AND localStorage has data —
// the idempotency rules out re-importing across re-runs and prevents
// clobbering whatever the bridge already holds.
//
// Returns true if anything was migrated, so the settings store can log
// or react. Errors are swallowed: a half-migrated state is still better
// than crashing the renderer at module load.
export function migrateLocalStorageToBridge(): boolean {
  if (!isElectron()) return false
  const bridge = window.openpa?.auth
  if (!bridge) return false
  const snap = bridgeSnapshot()
  const patch: Partial<AuthSnapshot> = {}

  // Tokens: only copy keys the bridge doesn't already have. Per-profile
  // merge means a re-run with new profiles in localStorage still picks
  // up the additions.
  const lsProfiles = getLoggedInProfilesFromLocalStorage()
  const tokensToCopy: Record<string, string> = {}
  for (const p of lsProfiles) {
    if (snap.tokens[p]) continue
    const t = localStorage.getItem(`agent_token_${p}`)
    if (t) tokensToCopy[p] = t
  }
  if (Object.keys(tokensToCopy).length > 0) {
    patch.tokens = tokensToCopy
  }

  // Logged-in profiles: union with whatever's in the bridge.
  if (lsProfiles.length > 0) {
    const merged = Array.from(new Set([...snap.loggedInProfiles, ...lsProfiles]))
    if (merged.length !== snap.loggedInProfiles.length) {
      patch.loggedInProfiles = merged
    }
  }

  // Active profile: bridge wins if set; otherwise adopt localStorage.
  if (!snap.activeProfileId) {
    const ls = localStorage.getItem('profile_id')
    if (ls) patch.activeProfileId = ls
  }

  // Reasoning toggles: copy each profile's flag if the bridge doesn't
  // have a value for it.
  const reasoningPatch: Record<string, boolean> = {}
  for (const p of lsProfiles) {
    if (p in snap.reasoningEnabled) continue
    const raw = localStorage.getItem(`reasoning_enabled_${p}`)
    if (raw === 'true' || raw === 'false') {
      reasoningPatch[p] = raw === 'true'
    }
  }
  if (Object.keys(reasoningPatch).length > 0) {
    patch.reasoningEnabled = reasoningPatch
  }

  if (Object.keys(patch).length === 0) return false

  // Fire-and-forget — the local snapshot won't update until the IPC
  // resolves, but the very next read (after the user does something
  // else) sees the merged value. For the immediate post-migration read
  // in settings.ts's setup function, we patch the in-memory snapshot
  // synchronously to avoid a flash of "no token".
  const optimistic: AuthSnapshot = {
    tokens: { ...snap.tokens, ...(patch.tokens ?? {}) },
    loggedInProfiles: patch.loggedInProfiles ?? snap.loggedInProfiles,
    activeProfileId: patch.activeProfileId ?? snap.activeProfileId,
    reasoningEnabled: { ...snap.reasoningEnabled, ...(patch.reasoningEnabled ?? {}) },
  }
  if (window.openpa?.auth) {
    window.openpa.auth.snapshot = optimistic
  }
  void bridge.set(patch).then((next) => {
    if (window.openpa?.auth) {
      window.openpa.auth.snapshot = next
    }
  })

  // Drop the migrated keys from localStorage so reload-onto-new-origin
  // doesn't see stale copies later. We only remove keys we successfully
  // staged into the patch.
  try {
    for (const p of Object.keys(tokensToCopy)) {
      localStorage.removeItem(`agent_token_${p}`)
    }
    if (patch.loggedInProfiles) {
      localStorage.removeItem('logged_in_profiles')
    }
    if (patch.activeProfileId) {
      localStorage.removeItem('profile_id')
    }
    for (const p of Object.keys(reasoningPatch)) {
      localStorage.removeItem(`reasoning_enabled_${p}`)
    }
  } catch {
    // Storage quota / disabled storage — non-fatal, the bridge is the
    // source of truth now and the stale keys will be ignored next time
    // because the bridge slot is populated.
  }

  return true
}
