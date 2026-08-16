import { useState, useEffect } from 'react';
import { getReport } from '../api/client.js';
import Chatbot from './Chatbot.jsx';

/**
 * ReportViewer — Renders a completed research report.
 *
 * Fetches the report from GET /api/reports/:reportId and displays:
 *   - Query text + metadata (sources, timestamps)
 *   - Sentiment badge (overall: positive | negative | mixed | neutral)
 *   - Themes list
 *   - Verified claims with verdict badges, confidence bars, citations
 *
 * All field names match the REAL schema from:
 *   - schema.sql: reports table (sentiment_summary, themes, verified_claims JSONB)
 *   - summarize.py: writes { overall: "<label>" } to sentiment_summary
 *   - verify_claim.py: VerifiedClaimDict { claim, verdict, confidence, source_type, citations, justification }
 *   - citations: { url, title, snippet }
 *
 * NOTE (doc-vs-code discrepancy):
 *   schema.sql comment says sentiment_summary is { "positive": 62, "negative": 28, "neutral": 10 }
 *   but summarize.py actually writes { "overall": "positive" } — a single label, not percentages.
 *   We trust the code (summarize.py) over the schema comment.
 */

// ── Verdict configuration ────────────────────────────────────────────
const VERDICT_CONFIG = {
  supported: {
    label: 'Supported',
    icon: '✓',
    className: 'badge-supported',
    barColor: 'var(--verdict-supported)',
  },
  contradicted: {
    label: 'Contradicted',
    icon: '✗',
    className: 'badge-contradicted',
    barColor: 'var(--verdict-contradicted)',
  },
  unverified: {
    label: 'Unverified',
    icon: '?',
    className: 'badge-unverified',
    barColor: 'var(--verdict-unverified)',
  },
  disputed: {
    label: 'Disputed',
    icon: '⚡',
    className: 'badge-disputed',
    barColor: 'var(--verdict-disputed)',
  },
};

// ── Sentiment configuration ──────────────────────────────────────────
const SENTIMENT_CONFIG = {
  positive: { icon: '😊', className: 'sentiment-positive' },
  negative: { icon: '😟', className: 'sentiment-negative' },
  mixed: { icon: '🤔', className: 'sentiment-mixed' },
  neutral: { icon: '😐', className: 'sentiment-neutral' },
};

function formatDate(isoString) {
  if (!isoString) return '';
  return new Date(isoString).toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export default function ReportViewer({ reportId, onBack }) {
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [expandedClaims, setExpandedClaims] = useState(new Set());

  useEffect(() => {
    async function loadReport() {
      setLoading(true);
      setError('');
      try {
        const data = await getReport(reportId);
        setReport(data);
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    }
    if (reportId) loadReport();
  }, [reportId]);

  function toggleClaimCitations(idx) {
    setExpandedClaims((prev) => {
      const next = new Set(prev);
      if (next.has(idx)) {
        next.delete(idx);
      } else {
        next.add(idx);
      }
      return next;
    });
  }

  if (loading) {
    return (
      <div className="empty-state animate-fade-in">
        <span className="spinner" style={{ width: 28, height: 28 }} />
        <div className="empty-state-title" style={{ marginTop: 16 }}>
          Loading report…
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="glass-card animate-fade-in" style={{ textAlign: 'center' }}>
        <div style={{ fontSize: '2rem', marginBottom: 8 }}>⚠️</div>
        <h3>Failed to load report</h3>
        <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: 16 }}>
          {error}
        </p>
        {onBack && (
          <button className="btn btn-ghost" onClick={onBack}>
            ← Back
          </button>
        )}
      </div>
    );
  }

  if (!report) return null;

  // Extract report fields using REAL field names from the backend
  const sentimentLabel = report.sentiment_summary?.overall || 'neutral';
  const sentimentCfg = SENTIMENT_CONFIG[sentimentLabel] || SENTIMENT_CONFIG.neutral;
  const themes = report.themes || [];
  const verifiedClaims = report.verified_claims || [];
  const sourcesRequested = report.sources_requested || [];
  const sourcesFailed = report.sources_failed || [];
  const rawData = report.raw_data || [];

  return (
    <div className="animate-fade-in">
      {/* Header */}
      <div className="report-header">
        {onBack && (
          <button
            className="btn btn-ghost btn-sm"
            onClick={onBack}
            style={{ marginBottom: 12 }}
          >
            ← Back
          </button>
        )}
        <div className="report-query">&ldquo;{report.query_text}&rdquo;</div>
        <div className="report-meta">
          <span>📅 {formatDate(report.created_at)}</span>
          <span>
            📡 Sources: {sourcesRequested.join(', ')}
          </span>
          {sourcesFailed.length > 0 && (
            <span style={{ color: 'var(--verdict-contradicted)' }}>
              ⚠ Failed: {sourcesFailed.join(', ')}
            </span>
          )}
        </div>
      </div>

      {/* Sentiment */}
      <div className="report-section">
        <div className="report-section-title">Overall Sentiment</div>
        <div className={`sentiment-badge-large ${sentimentCfg.className}`}>
          <span>{sentimentCfg.icon}</span>
          <span>{sentimentLabel}</span>
        </div>
      </div>

      {/* Themes */}
      {themes.length > 0 && (
        <div className="report-section">
          <div className="report-section-title">Themes</div>
          <div className="themes-list">
            {themes.map((t, i) => (
              <div key={i} className="theme-chip">
                <span>🏷️</span>
                <span>{t.theme}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Verified Claims */}
      <div className="report-section">
        <div className="report-section-title">
          Verified Claims ({verifiedClaims.length})
        </div>

        {verifiedClaims.length === 0 && (
          <div className="glass-panel" style={{ textAlign: 'center', padding: 24 }}>
            <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>
              No claims were extracted for this report.
            </p>
          </div>
        )}

        {verifiedClaims.map((claim, idx) => {
          const vcfg = VERDICT_CONFIG[claim.verdict] || VERDICT_CONFIG.unverified;
          const citations = claim.citations || [];
          const isExpanded = expandedClaims.has(idx);

          return (
            <div key={idx} className="claim-card animate-fade-in" style={{ animationDelay: `${idx * 60}ms` }}>
              <div className="claim-header">
                <div className="claim-text">{claim.claim}</div>
                <span className={`badge ${vcfg.className}`}>
                  <span className="verdict-icon">{vcfg.icon}</span>
                  {vcfg.label}
                </span>
              </div>

              {/* Confidence bar */}
              <div className="claim-details">
                <span>
                  Confidence: {Math.round((claim.confidence || 0) * 100)}%
                </span>
                <span>
                  Source: {claim.source_type || 'unknown'}
                </span>
              </div>

              <div style={{ marginTop: 8 }}>
                <div className="confidence-bar">
                  <div
                    className="confidence-bar-fill"
                    style={{
                      width: `${(claim.confidence || 0) * 100}%`,
                      background: vcfg.barColor,
                    }}
                  />
                </div>
              </div>

              {/* Justification */}
              {claim.justification && (
                <div className="claim-justification">
                  {claim.justification}
                </div>
              )}

              {/* Citations toggle */}
              {citations.length > 0 && (
                <>
                  <button
                    className="btn btn-ghost btn-sm"
                    onClick={() => toggleClaimCitations(idx)}
                    style={{ marginTop: 10, fontSize: '0.75rem' }}
                  >
                    {isExpanded ? '▾ Hide' : '▸ Show'} {citations.length} source{citations.length !== 1 ? 's' : ''}
                  </button>

                  {isExpanded && (
                    <div className="citation-list animate-fade-in">
                      {citations.map((c, ci) => (
                        <div key={ci} className="citation-item">
                          <a
                            className="citation-title"
                            href={c.url}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            {c.title || c.url || 'Source'}
                          </a>
                          {c.snippet && (
                            <div className="citation-snippet">
                              &ldquo;{c.snippet}&rdquo;
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          );
        })}
      </div>

      {/* Raw Data */}
      {rawData.length > 0 && (
        <div className="report-section">
          <div className="report-section-title">Raw Data</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
            {rawData.map((data, idx) => (
              <div key={idx} className="glass-panel" style={{ padding: '16px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '8px' }}>
                  <strong>Source: <span style={{textTransform: 'capitalize'}}>{data.source}</span></strong>
                  <span style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>{formatDate(data.published_at || data.created_at)}</span>
                </div>
                {data.author && <div style={{ marginBottom: '8px', fontSize: '0.9rem', color: 'var(--text-muted)' }}>Author: {data.author}</div>}
                <div style={{ whiteSpace: 'pre-wrap', fontSize: '0.95rem' }}>
                  {data.text}
                </div>
                {data.url && (
                  <div style={{ marginTop: '12px' }}>
                    <a href={data.url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--primary-color)', fontSize: '0.85rem' }}>
                      View Original
                    </a>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* RAG Chatbot */}
      <Chatbot jobId={report.query_id} />
    </div>
  );
}
