/**
 * Subscribes to live agent events for a conversation over SSE.
 *
 * EventSource cannot send Authorization headers, so we use fetch + ReadableStream
 * and parse SSE frames manually. The backend sends frames of the shape:
 *
 *     data: {"seq": 12, "type": "thinking", "data": {...}}\n\n
 *
 * The first events after subscribing are a replay of the in-progress run (if
 * any). A `ready` event marks the boundary between replay and live tail.
 *
 * The connection itself is shared across browser tabs by `createSharedStream`
 * — only one tab actually opens the SSE for a given conversation; other tabs
 * receive events via BroadcastChannel. See `sharedStream.ts`.
 */

import { createSharedStream, type SharedStreamHandle, type SharedStreamRawHandle } from './sharedStream';

export interface ConversationStreamEvent {
  seq?: number;
  type:
    | 'ready'
    | 'user_message'
    | 'event_trigger_message'
    | 'thinking'
    | 'result'
    | 'text'
    | 'file'
    | 'terminal'
    | 'token_usage'
    | 'phase'
    | 'summary'
    | 'complete'
    | 'error'
    | 'cwd';
  data: any;
}

export type ConversationStreamHandle = SharedStreamHandle;

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function openConversationStreamRaw(
  agentUrl: string,
  authToken: string,
  conversationId: string,
  onEvent: (e: ConversationStreamEvent) => void,
  onError: (err: any) => void,
): SharedStreamRawHandle {
  const controller = new AbortController();
  let closed = false;
  let attempt = 0;
  const backoffs = [1000, 2000, 5000];

  const run = async () => {
    while (!closed) {
      try {
        const base = resolveBaseUrl(agentUrl);
        const url = `${base}/api/conversations/${encodeURIComponent(conversationId)}/stream`;
        const headers: Record<string, string> = { Accept: 'text/event-stream' };
        if (authToken) headers['Authorization'] = `Bearer ${authToken}`;

        console.log('[debug:convstream] fetching', { url, conversationId });
        const res = await fetch(url, { headers, signal: controller.signal });
        if (!res.ok || !res.body) {
          throw new Error(`SSE failed: ${res.status} ${res.statusText}`);
        }
        console.log('[debug:convstream] connected', { conversationId, status: res.status });
        attempt = 0;

        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';

        while (!closed) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          // SSE frames are separated by a blank line. Tolerate \r\n + \n.
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

            // Each frame may contain multiple `data:` lines. Concatenate them.
            const dataLines: string[] = [];
            for (const rawLine of frame.split(/\r?\n/)) {
              if (rawLine.startsWith('data:')) {
                dataLines.push(rawLine.slice(5).replace(/^ /, ''));
              }
              // Skip ":" comment lines (heartbeats) and other fields.
            }
            if (dataLines.length === 0) continue;

            try {
              const payload = JSON.parse(dataLines.join('\n')) as ConversationStreamEvent;
              console.log('[debug:convstream] frame', {
                conversationId,
                seq: payload.seq,
                type: payload.type,
              });
              onEvent(payload);
            } catch (err) {
              console.warn('[conversationStream] bad frame:', dataLines, err);
            }
          }
        }
        // Stream ended cleanly — exit. (Backend closed; nothing to retry on.)
        return;
      } catch (err: any) {
        if (closed || err?.name === 'AbortError') return;
        const wait = backoffs[Math.min(attempt, backoffs.length - 1)];
        attempt += 1;
        if (attempt > backoffs.length) {
          onError(err);
          return;
        }
        console.warn(`[conversationStream] reconnecting in ${wait}ms after error:`, err);
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

export function openConversationStream(
  agentUrl: string,
  authToken: string,
  conversationId: string,
  onEvent: (e: ConversationStreamEvent) => void,
  onError?: (e: any) => void,
): ConversationStreamHandle {
  return createSharedStream<ConversationStreamEvent>({
    key: `openpa:conv:${conversationId}`,
    bufferSize: 256,
    openRaw: (handleEvent, handleError) =>
      openConversationStreamRaw(agentUrl, authToken, conversationId, handleEvent, handleError),
    onEvent,
    onError,
  });
}
