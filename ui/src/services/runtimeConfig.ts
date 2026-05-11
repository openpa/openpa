/**
 * Resolves the OpenPA runtime config — single source of truth for callers
 * who need to know where the backend is, regardless of how the app was
 * launched.
 *
 * Precedence (highest first):
 *
 * 1. ``window.openpa.config`` — set by electron/preload.ts from the
 *    JSON file the installer writes. Authoritative under Electron.
 * 2. ``import.meta.env.VITE_AGENT_URL`` — build-time default for the
 *    standalone web build and the Vite dev server, where there's no
 *    Electron bridge.
 * 3. Same-origin heuristic for the Docker bundle: when the SPA is served
 *    on port 1515 by the openpa container, the backend is at the same
 *    host on port 1112. This makes the bundle work both when the user
 *    accesses it locally and when they open it on a remote server, with
 *    no per-deploy build flag.
 * 4. Empty string — the UI treats this as "not configured" and routes
 *    the user to the setup wizard.
 *
 * Writes go through ``setAgentUrl`` so the persisted config stays in sync
 * across renderer restarts. Under the web build this is a no-op (the URL
 * is fixed at build time); the wizard's "save URL" button is hidden in
 * that mode.
 */

const SPA_PORT = '1515'
const API_PORT = '1112'

export function getAgentUrl(): string {
  const fromBridge = window.openpa?.config?.agentUrl
  if (fromBridge) return fromBridge

  const fromEnv = import.meta.env.VITE_AGENT_URL as string | undefined
  if (fromEnv) return fromEnv

  // Docker-bundle heuristic: the SPA is served at <host>:1515 by the
  // openpa container, with the API exposed at <host>:1112 on the same
  // host. We swap the port to derive the API URL automatically.
  if (typeof window !== 'undefined' && window.location?.hostname) {
    if (window.location.port === SPA_PORT) {
      const protocol = window.location.protocol || 'http:'
      return `${protocol}//${window.location.hostname}:${API_PORT}`
    }
  }

  return ''
}

export async function setAgentUrl(url: string): Promise<void> {
  if (window.openpa) {
    await window.openpa.setConfig({ agentUrl: url })
  }
  // Web build has no persistent runtime store. The renderer keeps the
  // value in its own state for the session; on next reload it falls back
  // to VITE_AGENT_URL. That's the documented behavior for the web build —
  // operators set the URL at deploy time.
}

export function getDeploymentType(): 'local' | 'server' | '' {
  return window.openpa?.config?.deploymentType ?? ''
}
