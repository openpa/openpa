import {
  ClientFactory,
  ClientFactoryOptions,
  DefaultAgentCardResolver,
  JsonRpcTransportFactory,
  createAuthenticatingFetchWithRetry,
  type AuthenticationHandler,
} from '@a2a-js/sdk/client';
import { useSettingsStore } from '../stores/settings';
import { getAgentUrl } from './runtimeConfig';

// Declare Electron flag
declare const __IS_ELECTRON__: boolean;

// Get the agent URL — runtime config (Electron) or build-time fallback (web).
function getBaseUrl(): string {
  return getAgentUrl();
}

/**
 * Extracts the origin (protocol + host + port) from a URL.
 * The A2A agent card is served at {origin}/.well-known/agent-card.json,
 * so we need just the origin, not the full endpoint path.
 */
function extractOrigin(url: string): string {
  // If it's a relative path (e.g. '/a2a'), return just the origin of the current page
  if (url.startsWith('/')) {
    return window.location.origin;
  }
  try {
    const parsed = new URL(url);
    return parsed.origin;
  } catch {
    return url;
  }
}

/**
 * Resolves the agent server's origin for plain REST calls
 * (e.g. /api/tasks/{id}/cancel) outside the A2A SDK transport.
 */
export function getApiOrigin(): string {
  return extractOrigin(getBaseUrl());
}

/**
 * Creates an AuthenticationHandler that reads the active JWT token
 * from the settings store and injects it as a Bearer token.
 */
function createAuthHandler(): AuthenticationHandler {
  return {
    async headers(): Promise<Record<string, string>> {
      // Read the active token from the Pinia settings store
      const settingsStore = useSettingsStore();
      const token = settingsStore.authToken;
      if (token) {
        return { 'Authorization': `Bearer ${token}` };
      }
      return {};
    },
    async shouldRetryWithHeaders(_req: RequestInit, _res: Response) {
      // No automatic retry — user must regenerate token manually
      return undefined;
    },
  };
}

/**
 * Creates a ClientFactory with authentication support.
 * The authenticated fetch wrapper injects Bearer tokens into all requests.
 */
function createAuthenticatedFactory(): ClientFactory {
  const authHandler = createAuthHandler();
  const authFetch = createAuthenticatingFetchWithRetry(fetch, authHandler);

  return new ClientFactory(
    ClientFactoryOptions.createFrom(ClientFactoryOptions.default, {
      transports: [new JsonRpcTransportFactory({ fetchImpl: authFetch })],
      cardResolver: new DefaultAgentCardResolver({ fetchImpl: authFetch }),
    })
  );
}

// Export a function that returns the client promise with the current URL
export const getA2AClient = () => {
  const fullUrl = getBaseUrl();
  const origin = extractOrigin(fullUrl);
  console.log('Creating A2A client with URL:', origin);
  const factory = createAuthenticatedFactory();
  return factory.createFromUrl(origin);
};
