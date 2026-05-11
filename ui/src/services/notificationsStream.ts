/**
 * Subscribes to live event-driven notifications for the caller's profile over SSE.
 *
 * Mirrors {@link openConversationStream}: EventSource cannot send Authorization
 * headers, so we use fetch + ReadableStream and parse SSE frames manually.
 *
 * The first frames after subscribing replay any buffered entries newer than
 * the `since` cursor (catching up after a reconnect), followed by a `ready`
 * marker, followed by the live tail. The cursor is advanced as entries arrive
 * so subsequent reconnects only replay what was missed.
 *
 * The connection is shared across browser tabs by `createSharedStream` — only
 * the leader tab opens the SSE; others receive entries via BroadcastChannel.
 */

import type { EventNotificationEntry } from './skillEventsApi';
import { createSharedStream, type SharedStreamHandle, type SharedStreamRawHandle } from './sharedStream';

export type NotificationStreamHandle = SharedStreamHandle;

interface NotificationFrame {
  type: 'ready' | 'notification';
  data: EventNotificationEntry | Record<string, never>;
}

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function openNotificationsStreamRaw(
  agentUrl: string,
  authToken: string,
  sinceMs: number,
  onNotification: (entry: EventNotificationEntry) => void,
  onError: (err: any) => void,
): SharedStreamRawHandle {
  const controller = new AbortController();
  let closed = false;
  let attempt = 0;
  let cursor = sinceMs;
  const backoffs = [1000, 2000, 5000];

  const run = async () => {
    while (!closed) {
      try {
        const base = resolveBaseUrl(agentUrl);
        const url = `${base}/api/skill-events/notifications/stream?since=${encodeURIComponent(String(cursor))}`;
        const headers: Record<string, string> = { Accept: 'text/event-stream' };
        if (authToken) headers['Authorization'] = `Bearer ${authToken}`;

        console.log('[debug:notifstream] fetching', { url });
        const res = await fetch(url, { headers, signal: controller.signal });
        if (!res.ok || !res.body) {
          throw new Error(`SSE failed: ${res.status} ${res.statusText}`);
        }
        console.log('[debug:notifstream] connected', { status: res.status });
        attempt = 0;

        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';

        while (!closed) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          let idx: number;
          while (
            (idx = (() => {
              const a = buffer.indexOf('\n\n');
              const b = buffer.indexOf('\r\n\r\n');
              if (a === -1) return b;
              if (b === -1) return a;
              return Math.min(a, b);
            })()) !== -1
          ) {
            const sep = buffer[idx] === '\r' ? 4 : 2;
            const frame = buffer.slice(0, idx);
            buffer = buffer.slice(idx + sep);

            const dataLines: string[] = [];
            for (const rawLine of frame.split(/\r?\n/)) {
              if (rawLine.startsWith('data:')) {
                dataLines.push(rawLine.slice(5).replace(/^ /, ''));
              }
            }
            if (dataLines.length === 0) continue;

            try {
              const payload = JSON.parse(dataLines.join('\n')) as NotificationFrame;
              console.log('[debug:notifstream] frame', { type: payload.type });
              if (payload.type === 'notification') {
                const entry = payload.data as EventNotificationEntry;
                cursor = Math.max(cursor, entry.created_at);
                onNotification(entry);
              }
            } catch (err) {
              console.warn('[notificationsStream] bad frame:', dataLines, err);
            }
          }
        }
        return;
      } catch (err: any) {
        if (closed || err?.name === 'AbortError') return;
        const wait = backoffs[Math.min(attempt, backoffs.length - 1)];
        attempt += 1;
        if (attempt > backoffs.length) {
          onError(err);
          return;
        }
        console.warn(`[notificationsStream] reconnecting in ${wait}ms after error:`, err);
        await new Promise(r => setTimeout(r, wait));
      }
    }
  };

  run();

  return {
    close() {
      if (closed) return;
      closed = true;
      controller.abort();
    },
  };
}

export function openNotificationsStream(
  agentUrl: string,
  authToken: string,
  sinceMs: number,
  onNotification: (entry: EventNotificationEntry) => void,
  onError?: (e: any) => void,
): NotificationStreamHandle {
  return createSharedStream<EventNotificationEntry>({
    key: 'openpa:notifications',
    bufferSize: 64,
    openRaw: (handleEvent, handleError) =>
      openNotificationsStreamRaw(agentUrl, authToken, sinceMs, handleEvent, handleError),
    onEvent: onNotification,
    onError,
  });
}
