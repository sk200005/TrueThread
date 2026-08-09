/**
 * api/client.js — Centralized API client with JWT auth.
 *
 * All API calls go through this module. JWT token is stored in localStorage
 * under 're-search-token'. The base URL defaults to http://localhost:3000
 * and can be overridden via VITE_API_BASE_URL env var.
 */

const BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:3000';

// ── Token management ────────────────────────────────────────────────

const TOKEN_KEY = 're-search-token';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

// ── Generic fetch wrapper ───────────────────────────────────────────

async function apiFetch(path, options = {}) {
  const token = getToken();
  const headers = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...options.headers,
  };

  const res = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers,
  });

  if (res.status === 401) {
    clearToken();
    throw new Error('Session expired. Please log in again.');
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Request failed (${res.status})`);
  }

  // 204 No Content
  if (res.status === 204) return null;

  return res.json();
}

// ── Auth ─────────────────────────────────────────────────────────────

/**
 * POST /api/auth/login
 * @returns {{ token: string, user: { id, email, name } }}
 */
export async function login(email, password) {
  const data = await apiFetch('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  });
  setToken(data.token);
  return data;
}

/**
 * POST /api/auth/signup
 * @returns {{ token: string, user: { id, email, name, created_at } }}
 */
export async function signup(email, password, name) {
  const data = await apiFetch('/api/auth/signup', {
    method: 'POST',
    body: JSON.stringify({ email, password, name: name || undefined }),
  });
  setToken(data.token);
  return data;
}

export function logout() {
  clearToken();
}

// ── Queries ──────────────────────────────────────────────────────────

/**
 * POST /api/queries
 * @param {string} queryText
 * @param {string[]} [sources] - defaults to ['reddit', 'youtube']
 * @returns {{ jobId: string, status: 'pending', query: string, message: string }}
 */
export async function submitQuery(queryText, sources) {
  return apiFetch('/api/queries', {
    method: 'POST',
    body: JSON.stringify({
      queryText,
      ...(sources && sources.length > 0 ? { sources } : {}),
    }),
  });
}

/**
 * GET /api/queries/:jobId/status
 * @returns {{ id, query_text, status, sources_requested, sources_failed, created_at, completed_at }}
 */
export async function getJobStatus(jobId) {
  return apiFetch(`/api/queries/${jobId}/status`);
}

/**
 * POST /api/queries/:jobId/retry
 * @returns {{ jobId, status: 'pending', message }}
 */
export async function retryJob(jobId) {
  return apiFetch(`/api/queries/${jobId}/retry`, {
    method: 'POST',
  });
}

/**
 * POST /api/queries/:jobId/stop
 * @returns {{ jobId, status: 'cancelled', message }}
 */
export async function stopJob(jobId) {
  return apiFetch(`/api/queries/${jobId}/stop`, {
    method: 'POST',
  });
}

// ── Reports ──────────────────────────────────────────────────────────

/**
 * GET /api/reports
 * @returns {Array<{ id, query_id, query_text, sentiment_summary, created_at }>}
 */
export async function listReports() {
  return apiFetch('/api/reports');
}

/**
 * GET /api/reports/:reportId
 * @returns {{ id, query_id, query_text, sources_requested, sources_failed,
 *             sentiment_summary, themes, verified_claims, created_at }}
 */
export async function getReport(reportId) {
  return apiFetch(`/api/reports/${reportId}`);
}

// ── Streaming ────────────────────────────────────────────────────────

/**
 * Returns the full SSE stream URL for a job.
 * Used by the custom SSE client in sseClient.js.
 */
export function getStreamUrl(jobId) {
  return `${BASE_URL}/api/queries/${jobId}/stream`;
}
