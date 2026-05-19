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

// Diagnostic log. Mirrors the main-process auth-bridge.log so a tester
// can read both halves of the bridge (renderer + main) when something
// looks wrong. Prefixed with ``[auth]`` so it's easy to filter in
// DevTools.
function logAuth(event: string, detail?: unknown): void {
  try {
    if (detail === undefined) {
      console.log(`[auth] ${event}`)
    } else {
      console.log(`[auth] ${event}`, detail)
    }
  } catch {
    // console may not exist in some test harnesses.
  }
}

// Renderer-local cache of the auth state. Initialized once from the
// preload-injected snapshot and then maintained in-renderer by every
// write path. We cannot use ``window.openpa.auth.snapshot`` itself for
// this — contextBridge deep-clones + freezes the exposed object before
// handing it to the renderer's world, so writes like
// ``window.openpa.auth.snapshot = x`` throw a TypeError in strict mode
// (which is the default for ES modules and TS output). The throw
// happens synchronously before any ``await bridge.set(patch)`` line,
// so the IPC never fires and the disk file stays empty — exactly the
// symptom that reached production in v0.1.9-test34. Keeping the cache
// in a module variable sidesteps the freeze.
function normalizeSnapshot(raw: Partial<AuthSnapshot> | undefined | null): AuthSnapshot {
  if (!raw) return { ...EMPTY_SNAPSHOT, tokens: {}, loggedInProfiles: [], reasoningEnabled: {} }
  return {
    tokens: raw.tokens ?? {},
    loggedInProfiles: raw.loggedInProfiles ?? [],
    activeProfileId: raw.activeProfileId ?? '',
    reasoningEnabled: raw.reasoningEnabled ?? {},
  }
}

let cachedSnapshot: AuthSnapshot = isElectron()
  ? normalizeSnapshot(window.openpa?.auth?.snapshot as Partial<AuthSnapshot> | undefined)
  : { ...EMPTY_SNAPSHOT, tokens: {}, loggedInProfiles: [], reasoningEnabled: {} }

logAuth('init', {
  isElectron: isElectron(),
  bridgePresent: typeof window !== 'undefined' && !!window.openpa?.auth,
  initialProfiles: cachedSnapshot.loggedInProfiles,
  initialTokenKeys: Object.keys(cachedSnapshot.tokens),
  initialActiveProfileId: cachedSnapshot.activeProfileId,
})

function bridgeSnapshot(): AuthSnapshot {
  return cachedSnapshot
}

async function applyPatch(patch: Partial<AuthSnapshot>): Promise<void> {
  const bridge = window.openpa?.auth
  if (!bridge) {
    logAuth('applyPatch.noBridge', patch)
    return
  }
  logAuth('applyPatch.start', {
    tokenKeys: patch.tokens ? Object.keys(patch.tokens) : undefined,
    loggedInProfiles: patch.loggedInProfiles,
    activeProfileId: patch.activeProfileId,
  })
  // Optimistic update of the renderer-local cache so sync reads that
  // immediately follow the write — e.g. ``setTokenForProfile`` then
  // ``activateProfile`` inside SetupWizard.handleFinish — observe the
  // new value without waiting for the IPC round-trip.
  cachedSnapshot = {
    tokens: patch.tokens ? { ...cachedSnapshot.tokens, ...patch.tokens } : cachedSnapshot.tokens,
    loggedInProfiles: patch.loggedInProfiles ?? cachedSnapshot.loggedInProfiles,
    activeProfileId: patch.activeProfileId ?? cachedSnapshot.activeProfileId,
    reasoningEnabled: patch.reasoningEnabled
      ? { ...cachedSnapshot.reasoningEnabled, ...patch.reasoningEnabled }
      : cachedSnapshot.reasoningEnabled,
  }
  // Reconcile with whatever the main process actually saved.
  try {
    const next = await bridge.set(patch)
    cachedSnapshot = normalizeSnapshot(next as Partial<AuthSnapshot>)
    logAuth('applyPatch.done', {
      profiles: cachedSnapshot.loggedInProfiles,
      tokenKeys: Object.keys(cachedSnapshot.tokens),
      activeProfileId: cachedSnapshot.activeProfileId,
    })
  } catch (err) {
    logAuth('applyPatch.error', { error: String(err) })
    throw err
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
  logAuth('setToken', { profile, tokenLength: token.length, electron: isElectron() })
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
  logAuth('removeToken', { profile, electron: isElectron() })
  if (isElectron()) {
    const bridge = window.openpa?.auth
    if (!bridge) return
    // Optimistic update for the same reason as applyPatch — callers
    // may sync-read getLoggedInProfiles right after removeToken.
    const remainingTokens = { ...cachedSnapshot.tokens }
    delete remainingTokens[profile]
    cachedSnapshot = {
      ...cachedSnapshot,
      tokens: remainingTokens,
      loggedInProfiles: cachedSnapshot.loggedInProfiles.filter((p) => p !== profile),
    }
    const next = await bridge.removeToken(profile)
    cachedSnapshot = normalizeSnapshot(next as Partial<AuthSnapshot>)
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
  if (!bridge) {
    logAuth('migrate.noBridge')
    return false
  }
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

  if (Object.keys(patch).length === 0) {
    logAuth('migrate.noop')
    return false
  }
  logAuth('migrate.applying', {
    tokenKeys: patch.tokens ? Object.keys(patch.tokens) : undefined,
    loggedInProfiles: patch.loggedInProfiles,
    activeProfileId: patch.activeProfileId,
  })

  // Optimistic update of the renderer-local cache so the settings
  // store's module-init read picks up the migrated values in the same
  // tick. The IPC round-trip persists to disk in the background.
  cachedSnapshot = {
    tokens: { ...cachedSnapshot.tokens, ...(patch.tokens ?? {}) },
    loggedInProfiles: patch.loggedInProfiles ?? cachedSnapshot.loggedInProfiles,
    activeProfileId: patch.activeProfileId ?? cachedSnapshot.activeProfileId,
    reasoningEnabled: { ...cachedSnapshot.reasoningEnabled, ...(patch.reasoningEnabled ?? {}) },
  }
  void bridge.set(patch).then((next) => {
    cachedSnapshot = normalizeSnapshot(next as Partial<AuthSnapshot>)
    logAuth('migrate.done', {
      profiles: cachedSnapshot.loggedInProfiles,
      tokenKeys: Object.keys(cachedSnapshot.tokens),
    })
  }).catch((err: unknown) => {
    logAuth('migrate.error', { error: String(err) })
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
