import { useEffect, useReducer, useRef, useState, useCallback } from 'react';
import { createSSEConnection } from '../api/sseClient.js';
import { retryJob, stopJob } from '../api/client.js';
import { updateJob } from '../api/jobHistory.js';

/**
 * LiveStatusPanel — Real-time pipeline progress via SSE.
 *
 * Opens a streaming connection to GET /api/queries/:jobId/stream.
 * Renders two sections:
 *
 *   1. Source Lanes (parallel, research pipeline):
 *      reddit · youtube · wikipedia
 *
 *   2. Processing Stages (sequential, query pipeline):
 *      rag_retrieve → extract_claims → classify_claims →
 *      [news_verify | wiki_verify] → verify_claim → summarize
 *
 * Event source names come directly from the actual Python pipeline code:
 *   - research_graph.py emits: source = "wikipedia" | "reddit" | "youtube"
 *   - query_graph.py emits: source = "rag_retrieve" | "extract_claims" |
 *     "classify_claims" | "news_verify" | "wiki_verify" | "verify_claim" | "summarize"
 *
 * Mock data detection:
 *   reddit_node.py and youtube_node.py are placeholder implementations
 *   (return dummy data, not real API calls). There is NO is_mock flag in
 *   the event payloads. We use the node name as a proxy — marking reddit
 *   and youtube as "mock data" in the UI. This is a KNOWN GAP; when
 *   real implementations are added, this heuristic should be replaced
 *   with an explicit flag from the event payload.
 */

// ── Source lanes (research pipeline) ─────────────────────────────────
const SOURCE_LANES = [
  { id: 'reddit', label: 'Reddit', icon: '💬', isMock: true },
  { id: 'youtube', label: 'YouTube', icon: '📺', isMock: true },
  { id: 'wikipedia', label: 'Wikipedia', icon: '📚', isMock: false },
];

// ── Processing stages (query pipeline) — sequential order ────────────
const PROCESSING_STAGES = [
  { id: 'rag_retrieve', label: 'RAG Retrieve', icon: '🔎', countKey: 'chunksRetrieved' },
  { id: 'extract_claims', label: 'Extract Claims', icon: '📝', countKey: 'claimsExtracted' },
  { id: 'classify_claims', label: 'Classify Claims', icon: '🏷️', countKey: 'claimsClassified' },
  { id: 'news_verify', label: 'News Verify', icon: '📰', countKey: 'newsEvidenceCount', conditional: true },
  { id: 'wiki_verify', label: 'Wiki Verify', icon: '📖', countKey: 'wikiEvidenceCount', conditional: true },
  { id: 'verify_claim', label: 'Verify Claims', icon: '⚖️', countKey: 'claimsVerified' },
  { id: 'summarize', label: 'Summarize', icon: '📊' },
];

// ── State reducer ────────────────────────────────────────────────────

const initialState = {
  connection: 'connecting', // 'connecting' | 'connected' | 'disconnected'
  sources: {},              // { reddit: { status, counts }, ... }
  stages: {},               // { rag_retrieve: { status, counts }, ... }
  terminal: null,           // { type: 'done' | 'error', results?, error? }
  error: null,
};

function reducer(state, action) {
  switch (action.type) {
    case 'CONNECTION_CHANGE':
      return { ...state, connection: action.status };

    case 'SSE_EVENT': {
      const evt = action.event;

      // Terminal events
      if (evt.type === 'done') {
        return { ...state, terminal: { type: 'done', results: evt.results } };
      }
      if (evt.type === 'error') {
        return { ...state, terminal: { type: 'error', error: evt.error } };
      }
      if (evt.type === 'cancelled') {
        return { ...state, terminal: { type: 'cancelled' } };
      }

      // Connected event
      if (evt.type === 'connected') {
        return { ...state, connection: 'connected' };
      }

      // Progress events — route to source lanes or processing stages
      if (evt.type === 'progress' && evt.source) {
        const isSourceLane = SOURCE_LANES.some((s) => s.id === evt.source);

        if (isSourceLane) {
          return {
            ...state,
            sources: {
              ...state.sources,
              [evt.source]: {
                status: evt.status || 'started',
                counts: evt.counts || state.sources[evt.source]?.counts,
              },
            },
          };
        }

        // Processing stage
        return {
          ...state,
          stages: {
            ...state.stages,
            [evt.source]: {
              status: evt.status || 'started',
              counts: evt.counts || state.stages[evt.source]?.counts,
            },
          },
        };
      }

      return state;
    }

    case 'FATAL_ERROR':
      return { ...state, error: action.message, connection: 'disconnected' };

    case 'RESET':
      return { ...initialState };

    default:
      return state;
  }
}

// ── Status icon helper ──────────────────────────────────────────────

function StatusIcon({ status }) {
  switch (status) {
    case 'started':
      return <span className="spinner" />;
    case 'done':
      return <span style={{ color: 'var(--status-done)' }}>✓</span>;
    case 'failed':
    case 'error':
      return <span style={{ color: 'var(--status-failed)' }}>✗</span>;
    case 'degraded':
      return <span style={{ color: 'var(--status-degraded)' }}>⚠</span>;
    default:
      return <span style={{ color: 'var(--status-pending)', opacity: 0.5 }}>○</span>;
  }
}

function getStageClass(status) {
  if (status === 'started') return 'active';
  if (status === 'done') return 'done';
  if (status === 'failed' || status === 'error') return 'failed';
  if (status === 'degraded') return 'degraded';
  return '';
}

// ── Component ────────────────────────────────────────────────────────

export default function LiveStatusPanel({ jobId, onDone, onViewReport }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const sseRef = useRef(null);
  const [retrying, setRetrying] = useState(false);

  const startStream = useCallback(() => {
    dispatch({ type: 'RESET' });

    sseRef.current = createSSEConnection(jobId, {
      onEvent: (event) => {
        dispatch({ type: 'SSE_EVENT', event });

        // Update localStorage status tracking
        if (event.type === 'done') {
          updateJob(jobId, { status: 'done' });
        } else if (event.type === 'error') {
          updateJob(jobId, { status: 'error' });
        } else if (event.type === 'progress') {
          updateJob(jobId, { status: 'running' });
        }
      },
      onConnectionChange: (status) => {
        dispatch({ type: 'CONNECTION_CHANGE', status });
      },
      onError: (message) => {
        dispatch({ type: 'FATAL_ERROR', message });
      },
    });
  }, [jobId]);

  useEffect(() => {
    startStream();
    return () => {
      sseRef.current?.close();
    };
  }, [startStream]);

  // Notify parent when the job completes
  useEffect(() => {
    if (state.terminal?.type === 'done') {
      onDone?.();
    }
  }, [state.terminal, onDone]);

  async function handleRetry() {
    setRetrying(true);
    try {
      await retryJob(jobId);
      updateJob(jobId, { status: 'pending' });
      // Restart SSE stream
      sseRef.current?.close();
      startStream();
    } catch (err) {
      dispatch({ type: 'FATAL_ERROR', message: err.message });
    } finally {
      setRetrying(false);
    }
  }

  async function handleStop() {
    try {
      await stopJob(jobId);
      updateJob(jobId, { status: 'cancelled' });
      sseRef.current?.close();
      dispatch({ type: 'SSE_EVENT', event: { type: 'cancelled' } });
    } catch (err) {
      console.error('Failed to stop job:', err);
    }
  }

  function handleReconnect() {
    sseRef.current?.close();
    startStream();
  }

  const connectionBannerClass =
    state.connection === 'connected'
      ? 'connected'
      : state.connection === 'connecting'
        ? 'connecting'
        : 'disconnected';

  // Filter out connectors after conditional stages that aren't shown
  const visibleStages = PROCESSING_STAGES.filter(
    (stage) => !stage.conditional || state.stages[stage.id]
  );

  return (
    <div className="animate-fade-in">
      <h2 style={{ marginBottom: 16 }}>Live Pipeline Status</h2>

      {/* Connection banner */}
      <div className={`connection-banner ${connectionBannerClass}`}>
        {state.connection === 'connecting' && (
          <>
            <span className="spinner" /> Connecting to stream…
            {!state.terminal && (
              <button className="btn btn-ghost btn-sm" onClick={handleStop} style={{ marginLeft: 'auto', color: 'var(--status-failed)' }}>
                Stop
              </button>
            )}
          </>
        )}
        {state.connection === 'connected' && (
          <>
            <span className="animate-pulse-dot">●</span> Connected — streaming live updates
            {!state.terminal && (
              <button className="btn btn-ghost btn-sm" onClick={handleStop} style={{ marginLeft: 'auto', color: 'var(--status-failed)' }}>
                Stop
              </button>
            )}
          </>
        )}
        {state.connection === 'disconnected' && (
          <>
            <span>●</span> Connection lost
            {!state.error && (
              <button className="btn btn-ghost btn-sm" onClick={handleReconnect} style={{ marginLeft: 'auto' }}>
                Reconnect
              </button>
            )}
          </>
        )}
      </div>

      {state.error && (
        <div className="auth-error" style={{ marginBottom: 16 }}>
          {state.error}
        </div>
      )}

      {/* Source Lanes (parallel) */}
      <div className="pipeline-section">
        <h3>Data Sources</h3>
        <div className="source-lanes">
          {SOURCE_LANES.map((lane) => {
            const sourceState = state.sources[lane.id];
            const status = sourceState?.status || 'pending';

            return (
              <div key={lane.id} className={`stage-card ${getStageClass(status)}`}>
                <div className={`stage-icon ${status}`}>
                  <StatusIcon status={status} />
                </div>
                <div className="stage-info">
                  <div className="stage-name">
                    {lane.icon} {lane.label}
                  </div>
                  <div className="stage-meta">
                    {status === 'pending' && 'Waiting…'}
                    {status === 'started' && 'Fetching…'}
                    {status === 'done' && 'Complete'}
                    {(status === 'failed' || status === 'error') && 'Failed'}
                    {/*
                      KNOWN GAP: reddit and youtube nodes are placeholder implementations
                      that return mock data. There is no is_mock flag in the event payload.
                      We use the node name as a proxy to show a "Mock" indicator.
                      Replace this heuristic with an explicit payload flag when real
                      implementations are added.
                    */}
                    {lane.isMock && status === 'done' && (
                      <span className="mock-indicator" style={{ marginLeft: 6 }}>Mock</span>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Processing Stages (sequential) */}
      <div className="pipeline-section">
        <h3>Analysis Pipeline</h3>
        {visibleStages.map((stage, idx) => {
          const stageState = state.stages[stage.id];
          const status = stageState?.status || 'pending';

          const countKey = stage.countKey;
          const countValue = stageState?.counts?.[countKey];

          return (
            <div key={stage.id}>
              <div className={`stage-card ${getStageClass(status)}`} style={{ marginBottom: 0 }}>
                <div className={`stage-icon ${status}`}>
                  <StatusIcon status={status} />
                </div>
                <div className="stage-info">
                  <div className="stage-name">
                    {stage.icon} {stage.label}
                  </div>
                  <div className="stage-meta">
                    {status === 'pending' && 'Waiting…'}
                    {status === 'started' && 'Processing…'}
                    {status === 'done' && (
                      <>
                        Complete
                        {countValue !== undefined && (
                          <span style={{ marginLeft: 6, color: 'var(--text-accent)' }}>
                            ({countValue})
                          </span>
                        )}
                      </>
                    )}
                    {(status === 'failed' || status === 'error') && 'Failed'}
                  </div>
                </div>
              </div>
              {/* Connector arrow between stages */}
              {idx < visibleStages.length - 1 && (
                <div className="stage-connector">↓</div>
              )}
            </div>
          );
        })}
      </div>

      {/* Terminal state: Done */}
      {state.terminal?.type === 'done' && (
        <div className="glass-card animate-fade-in" style={{ textAlign: 'center', marginTop: 16 }}>
          <div style={{ fontSize: '2rem', marginBottom: 8 }}>✅</div>
          <h3>Research Complete</h3>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: 16 }}>
            Your research report is ready to view.
          </p>
          <button className="btn btn-primary" onClick={onViewReport} id="view-report-btn">
            📊 View Report
          </button>
        </div>
      )}

      {/* Terminal state: Error */}
      {state.terminal?.type === 'error' && (
        <div className="glass-card animate-fade-in" style={{ textAlign: 'center', marginTop: 16 }}>
          <div style={{ fontSize: '2rem', marginBottom: 8 }}>❌</div>
          <h3>Pipeline Error</h3>
          <p
            style={{
              color: 'var(--text-muted)',
              fontSize: '0.85rem',
              marginBottom: 8,
              fontFamily: 'var(--font-mono)',
            }}
          >
            {state.terminal.error || 'An unknown error occurred.'}
          </p>
          <button
            className="btn btn-primary"
            onClick={handleRetry}
            disabled={retrying}
            id="retry-job-btn"
          >
            {retrying ? (
              <>
                <span className="spinner" /> Retrying…
              </>
            ) : (
              <>🔄 Retry Job</>
            )}
          </button>
        </div>
      )}

      {/* Terminal state: Cancelled */}
      {state.terminal?.type === 'cancelled' && (
        <div className="glass-card animate-fade-in" style={{ textAlign: 'center', marginTop: 16 }}>
          <div style={{ fontSize: '2rem', marginBottom: 8 }}>🛑</div>
          <h3>Pipeline Cancelled</h3>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: 16 }}>
            The pipeline processing was stopped manually.
          </p>
        </div>
      )}
    </div>
  );
}
