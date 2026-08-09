/**
 * api/sseClient.js — Custom SSE client using fetch + ReadableStream.
 *
 * Why not EventSource?
 *   The Node SSE endpoint (GET /api/queries/:jobId/stream) requires
 *   JWT auth via the Authorization header. The native EventSource API
 *   doesn't support custom headers. Rather than modifying the backend
 *   (which is out of scope), we implement SSE parsing manually using
 *   fetch() with a streaming body reader.
 *
 * Features:
 *   - JWT auth via Authorization header
 *   - Auto-reconnect with exponential backoff (1s → 2s → 4s, max 30s)
 *   - Max 5 retries, then permanent disconnect
 *   - Proper cleanup on abort
 *   - Handles SSE `data: <JSON>\n\n` format
 */

import { getToken, getStreamUrl } from './client.js';

/**
 * Create a managed SSE connection to a job's stream endpoint.
 *
 * @param {string} jobId - The job UUID to stream
 * @param {object} callbacks
 * @param {(event: object) => void} callbacks.onEvent - Called for each parsed SSE event
 * @param {(status: 'connecting'|'connected'|'disconnected') => void} callbacks.onConnectionChange
 * @param {(error: string) => void} callbacks.onError - Called on fatal error
 * @returns {{ close: () => void }} - Call close() to tear down the connection
 */
export function createSSEConnection(jobId, { onEvent, onConnectionChange, onError }) {
  let abortController = new AbortController();
  let retryCount = 0;
  let retryTimeout = null;
  let closed = false;

  const MAX_RETRIES = 5;
  const BASE_DELAY_MS = 1000;
  const MAX_DELAY_MS = 30000;

  async function connect() {
    if (closed) return;

    const token = getToken();
    if (!token) {
      onError?.('No auth token. Please log in.');
      return;
    }

    onConnectionChange?.('connecting');

    try {
      abortController = new AbortController();

      const res = await fetch(getStreamUrl(jobId), {
        headers: {
          Authorization: `Bearer ${token}`,
        },
        signal: abortController.signal,
      });

      if (!res.ok) {
        if (res.status === 401) {
          onError?.('Session expired. Please log in again.');
          return; // Don't retry auth failures
        }
        throw new Error(`Stream request failed (${res.status})`);
      }

      onConnectionChange?.('connected');
      retryCount = 0; // Reset on successful connection

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // Parse complete SSE messages from the buffer.
        // SSE format: "data: <JSON>\n\n"
        // Keepalives: ": keepalive\n\n" (lines starting with : are comments)
        const messages = buffer.split('\n\n');
        // Keep the last (possibly incomplete) chunk in the buffer
        buffer = messages.pop() || '';

        for (const msg of messages) {
          if (!msg.trim()) continue;

          // Skip SSE comments (keepalive pings)
          if (msg.trim().startsWith(':')) continue;

          // Extract data from "data: <JSON>" lines
          const lines = msg.split('\n');
          let dataStr = '';
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              dataStr += line.slice(6);
            } else if (line.startsWith('data:')) {
              dataStr += line.slice(5);
            }
          }

          if (!dataStr) continue;

          try {
            const event = JSON.parse(dataStr);
            onEvent?.(event);

            // Terminal events — don't reconnect
            if (event.type === 'done' || event.type === 'error') {
              closed = true;
              return;
            }
          } catch (parseErr) {
            console.warn('[SSE] Failed to parse event:', dataStr, parseErr);
          }
        }
      }

      // Stream ended naturally (server closed connection)
      if (!closed) {
        onConnectionChange?.('disconnected');
        scheduleReconnect();
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        // Expected when close() is called
        return;
      }

      console.warn('[SSE] Connection error:', err.message);
      if (!closed) {
        onConnectionChange?.('disconnected');
        scheduleReconnect();
      }
    }
  }

  function scheduleReconnect() {
    if (closed) return;

    retryCount++;
    if (retryCount > MAX_RETRIES) {
      onError?.('Connection lost. Max retries exceeded.');
      onConnectionChange?.('disconnected');
      return;
    }

    const delay = Math.min(BASE_DELAY_MS * Math.pow(2, retryCount - 1), MAX_DELAY_MS);
    console.log(`[SSE] Reconnecting in ${delay}ms (attempt ${retryCount}/${MAX_RETRIES})`);
    retryTimeout = setTimeout(connect, delay);
  }

  function close() {
    closed = true;
    if (retryTimeout) clearTimeout(retryTimeout);
    abortController.abort();
  }

  // Start the initial connection
  connect();

  return { close };
}
