/**
 * Subscribes to live conversation-list snapshots over SSE.
 *
 * The backend pushes a fresh ``{ conversations }`` snapshot whenever a
 * conversation is created, updated, deleted, or has a new message. Every
 * frame is the complete list for the caller's profile, so the client just
 * replaces its in-memory copy on each push.
 *
 * Mirrors {@link openSkillEventsAdminStream}: EventSource cannot send
 * Authorization headers, so we use fetch + ReadableStream and parse SSE
 * frames manually. Reconnects with exponential backoff are transparent.
 *
 * The connection is shared across browser tabs by `createSharedStream`.
 */

import type { ConversationSummary } from './conversationApi';
import { createSharedStream, type SharedStreamHandle, type SharedStreamRawHandle } from './sharedStream';

export type ConversationsListStreamHandle = SharedStreamHandle;

export interface ConversationsListSnapshot {
  conversations: ConversationSummary[];
}

interface ListFrame {
  type: 'snapshot' | 'ready';
  data: Partial<ConversationsListSnapshot>;
}

function resolveBaseUrl(agentUrl: string): string {
  if (agentUrl.startsWith('http://') || agentUrl.startsWith('https://')) {
    return agentUrl;
  }
  return `${window.location.origin}${agentUrl}`;
}

function openConversationsListStreamRaw(
  agentUrl: string,
  authToken: string,
  channelType: string | null,
  onSnapshot: (snap: ConversationsListSnapshot) => void,
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
        const qs = channelType ? `?channel_type=${encodeURIComponent(channelType)}` : '';
        const url = `${base}/api/conversations/stream${qs}`;
        const headers: Record<string, string> = { Accept: 'text/event-stream' };
        if (authToken) headers['Authorization'] = `Bearer ${authToken}`;

        const res = await fetch(url, { headers, signal: controller.signal });
        if (!res.ok || !res.body) {
          throw new Error(`SSE failed: ${res.status} ${res.statusText}`);
        }
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
              const payload = JSON.parse(dataLines.join('\n')) as ListFrame;
              if (payload.type === 'snapshot') {
                onSnapshot({
                  conversations: payload.data.conversations ?? [],
                });
              }
            } catch (err) {
              console.warn('[conversationsListStream] bad frame:', dataLines, err);
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
        console.warn(`[conversationsListStream] reconnecting in ${wait}ms after error:`, err);
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

export function openConversationsListStream(
  agentUrl: string,
  authToken: string,
  profileKey: string,
  channelType: string | null,
  onSnapshot: (snap: ConversationsListSnapshot) => void,
  onError?: (e: any) => void,
): ConversationsListStreamHandle {
  // Filter is part of the shared-stream key — different filters land in
  // separate leader/follower groups so they don't clobber each other when
  // multiple tabs view different channels.
  const filterKey = channelType ?? 'all';
  return createSharedStream<ConversationsListSnapshot>({
    key: `openpa:conversations:${profileKey}:${filterKey}`,
    bufferSize: 1,
    openRaw: (handleEvent, handleError) =>
      openConversationsListStreamRaw(agentUrl, authToken, channelType, handleEvent, handleError),
    onEvent: onSnapshot,
    onError,
  });
}
