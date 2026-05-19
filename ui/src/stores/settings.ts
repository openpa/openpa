import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import { fetchMe } from '../services/agentApi';
import { getAgentUrl, setAgentUrl as persistAgentUrl } from '../services/runtimeConfig';
import * as authStorage from '../services/authStorage';

export type ProfileValue = string | boolean | number | Record<string, unknown>;

function _migrateStorage() {
  // Legacy single-token format → per-profile format. Predates the
  // ``logged_in_profiles`` list, so the old keys lived directly in
  // localStorage as ``agent_auth_token`` + ``profile_id``. Promote to
  // the per-profile shape, but use the storage facade so the result
  // lands in the right place (main-process bridge in Electron;
  // localStorage in the web build).
  const oldToken = localStorage.getItem('agent_auth_token');
  const oldProfileId = localStorage.getItem('profile_id');
  if (oldToken && oldProfileId) {
    if (!authStorage.getToken(oldProfileId)) {
      void authStorage.setToken(oldProfileId, oldToken);
    }
    localStorage.removeItem('agent_auth_token');
  }
  // Earlier builds stored the agent URL in localStorage. The runtime config
  // (window.openpa.config) is now authoritative, so drop any stale value
  // here on first load to avoid confusion.
  localStorage.removeItem('agent_url');

  // Electron only: copy any tokens / loggedInProfiles / activeProfileId
  // / reasoning toggles that still live in renderer localStorage onto
  // the main-process bridge. Idempotent — only copies keys the bridge
  // doesn't already have, so re-running across upgrades is safe.
  // Without this, a user upgrading from a pre-bridge build would
  // appear logged out on the very first launch with new code.
  authStorage.migrateLocalStorageToBridge();
}

// Run migration on module load
_migrateStorage();

export const useSettingsStore = defineStore('settings', () => {
  // Auto-connect to agent on app load
  const autoConnect = ref(localStorage.getItem('auto_connect') === 'true');

  // Agent URL — runtime config (Electron) or build-time fallback (web).
  // ``setAgentUrl`` writes through to the persisted Electron config when
  // the bridge is available, so the value survives renderer restarts.
  const agentUrl = ref(getAgentUrl());

  async function setAgentUrlAction(url: string) {
    agentUrl.value = url;
    await persistAgentUrl(url);
  }

  // UI theme
  const theme = ref<'light' | 'dark'>(
    (localStorage.getItem('theme') as 'light' | 'dark') || 'light'
  );

  // Sidebar collapsed state
  const sidebarCollapsed = ref(localStorage.getItem('sidebar_collapsed') === 'true');

  // Active profile ID (the currently-used profile for this tab session).
  // Source is the cross-origin storage facade so a renderer that just
  // pivoted from file:// to http://localhost:1515 still knows which
  // profile the user was on.
  const profileId = ref(authStorage.getActiveProfileId());

  // Active authentication token (for the current profile session)
  const authToken = ref('');

  // Working directory from backend (for resolving absolute file paths to API URLs)
  const workingDir = ref('');

  // Reasoning toggle (per-profile, default: enabled)
  const reasoningEnabled = ref(true);

  // Detect if running in Electron
  const isElectron = computed(() => {
    return typeof __IS_ELECTRON__ !== 'undefined' && __IS_ELECTRON__;
  });

  // ── Desktop-app update preferences ──
  //
  // The release channel is fixed at install time (see app/upgrade/channel.py)
  // and is intentionally not exposed for runtime mutation — switching
  // channels is a deliberate reinstall. We only expose the ``autoUpdate``
  // toggle here. Source of truth is the Electron config (openpa-config.json),
  // read via the preload bridge.
  const bridgeConfig = (typeof window !== 'undefined' ? window.openpa?.config : undefined) ?? null;
  const autoUpdate = ref<boolean>(bridgeConfig?.autoUpdate ?? true);

  async function setAutoUpdate(value: boolean) {
    autoUpdate.value = value;
    if (typeof window !== 'undefined' && window.openpa) {
      await window.openpa.setConfig({ autoUpdate: value });
    }
  }

  // ── Per-profile token management ──
  //
  // All persistence routes through ``authStorage`` so the values survive
  // Chromium origin changes in Electron (file:// → http://localhost:1515).
  // The facade does an optimistic sync update of its snapshot before the
  // bridge IPC resolves, so callers can safely read back immediately
  // after writing — no need to await here.

  function getTokenForProfile(profileName: string): string {
    return authStorage.getToken(profileName);
  }

  function setTokenForProfile(profileName: string, token: string) {
    void authStorage.setToken(profileName, token);
  }

  function getReasoningEnabled(profileName: string): boolean {
    return authStorage.getReasoningEnabled(profileName);
  }

  function setReasoningEnabled(profileName: string, enabled: boolean) {
    reasoningEnabled.value = enabled;
    void authStorage.setReasoningEnabled(profileName, enabled);
  }

  function removeTokenForProfile(profileName: string) {
    void authStorage.removeToken(profileName);
  }

  function getLoggedInProfiles(): string[] {
    return authStorage.getLoggedInProfiles();
  }

  /**
   * Activate a profile for the current session.
   * Loads the stored token for this profile and sets it as active.
   * Returns true if the profile has a stored token.
   */
  function activateProfile(profileName: string): boolean {
    const token = getTokenForProfile(profileName);
    if (!token) return false;

    authToken.value = token;
    profileId.value = profileName;
    reasoningEnabled.value = getReasoningEnabled(profileName);

    fetchMe(agentUrl.value, token)
      .then((me) => {
        if (me.working_dir) {
          workingDir.value = me.working_dir;
        }
      })
      .catch(() => {});
    return true;
  }

  // ── Legacy methods ──

  function setAutoConnect(value: boolean) {
    autoConnect.value = value;
    localStorage.setItem('auto_connect', String(value));
  }

  function setTheme(newTheme: 'light' | 'dark') {
    theme.value = newTheme;
    localStorage.setItem('theme', newTheme);
  }

  function setSidebarCollapsed(value: boolean) {
    sidebarCollapsed.value = value;
    localStorage.setItem('sidebar_collapsed', String(value));
  }

  function setProfileId(id: string) {
    profileId.value = id;
    // Persist through the storage facade so the next launch / reload —
    // potentially on a different Chromium origin in Electron — knows
    // which profile to re-activate. Fire-and-forget: the local ref is
    // the source of truth for the current session.
    void authStorage.setActiveProfileId(id);
  }

  function setAuthToken(token: string) {
    authToken.value = token;
    if (token) {
      // Fetch profile from backend and update profileId + per-profile storage
      fetchMe(agentUrl.value, token)
        .then((me) => {
          if (me.profile) {
            setProfileId(me.profile);
            setTokenForProfile(me.profile, token);
          }
          if (me.working_dir) {
            workingDir.value = me.working_dir;
          }
        })
        .catch(() => {
          // Token may be invalid or server unreachable — ignore silently
        });
    } else {
      setProfileId('');
    }
  }

  return {
    autoConnect,
    agentUrl,
    setAgentUrl: setAgentUrlAction,
    theme,
    sidebarCollapsed,
    profileId,
    authToken,
    workingDir,
    isElectron,
    setAutoConnect,
    setTheme,
    setSidebarCollapsed,
    setProfileId,
    setAuthToken,
    // Per-profile token management
    getTokenForProfile,
    setTokenForProfile,
    removeTokenForProfile,
    getLoggedInProfiles,
    activateProfile,
    // Reasoning toggle
    reasoningEnabled,
    getReasoningEnabled,
    setReasoningEnabled,
    // Desktop-app update preferences
    autoUpdate,
    setAutoUpdate,
  };
});
