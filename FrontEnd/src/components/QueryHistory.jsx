import { useState, useEffect, useCallback } from 'react';
import { listReports } from '../api/client.js';
import { getJobs } from '../api/jobHistory.js';

/**
 * QueryHistory — Sidebar list of past jobs.
 *
 * Data sources:
 *   1. GET /api/reports — completed jobs (have reports)
 *   2. localStorage 're-search-jobs' — in-progress / errored jobs
 *
 * Since there's no GET /api/queries (list) endpoint on the backend,
 * localStorage is the fallback for tracking in-progress jobs.
 */

const STATUS_CONFIG = {
  pending: { label: 'Pending', className: 'badge-pending', icon: '⏳' },
  running: { label: 'Running', className: 'badge-running', icon: '⚡' },
  done: { label: 'Complete', className: 'badge-done', icon: '✓' },
  error: { label: 'Error', className: 'badge-error', icon: '✗' },
};

function formatTime(isoString) {
  if (!isoString) return '';
  const d = new Date(isoString);
  const now = new Date();
  const diffMs = now - d;
  const diffMin = Math.floor(diffMs / 60000);
  const diffHr = Math.floor(diffMs / 3600000);

  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  if (diffHr < 24) return `${diffHr}h ago`;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export default function QueryHistory({ activeJobId, onSelectJob, onSelectReport, refreshTrigger }) {
  const [reports, setReports] = useState([]);
  const [localJobs, setLocalJobs] = useState([]);
  const [loading, setLoading] = useState(false);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [reportsList, storedJobs] = await Promise.all([
        listReports().catch(() => []),
        Promise.resolve(getJobs()),
      ]);
      setReports(reportsList || []);
      setLocalJobs(storedJobs || []);
    } catch {
      // Silently fail — sidebar shouldn't block the main UI
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData, refreshTrigger]);

  // Merge: show local in-progress jobs that don't have a report yet
  const reportQueryIds = new Set(reports.map((r) => r.query_id));

  const inProgressJobs = localJobs.filter(
    (j) => !reportQueryIds.has(j.jobId) && j.status !== 'done'
  );

  const hasItems = inProgressJobs.length > 0 || reports.length > 0;

  return (
    <>
      {loading && inProgressJobs.length === 0 && reports.length === 0 && (
        <div style={{ padding: 16, textAlign: 'center' }}>
          <span className="spinner" />
        </div>
      )}

      {!loading && !hasItems && (
        <div className="empty-state" style={{ padding: '32px 16px' }}>
          <div className="empty-state-icon">📋</div>
          <div className="empty-state-title">No queries yet</div>
          <div className="empty-state-desc">
            Submit your first research query to get started.
          </div>
        </div>
      )}

      {/* In-progress / errored jobs (from localStorage) */}
      {inProgressJobs.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          <div
            style={{
              padding: '8px 14px',
              fontSize: '0.72rem',
              fontWeight: 600,
              color: 'var(--text-muted)',
              textTransform: 'uppercase',
              letterSpacing: '0.06em',
            }}
          >
            In Progress
          </div>
          {inProgressJobs.map((job) => {
            const cfg = STATUS_CONFIG[job.status] || STATUS_CONFIG.pending;
            return (
              <div
                key={job.jobId}
                className={`history-item ${activeJobId === job.jobId ? 'active' : ''}`}
                onClick={() => onSelectJob(job.jobId, job.queryText, job.status)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') onSelectJob(job.jobId, job.queryText, job.status);
                }}
              >
                <div className="history-item-query">{job.queryText}</div>
                <div className="history-item-meta">
                  <span className={`badge badge-status ${cfg.className}`}>
                    {cfg.icon} {cfg.label}
                  </span>
                  <span>{formatTime(job.createdAt)}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Completed jobs (from API reports) */}
      {reports.length > 0 && (
        <div>
          <div
            style={{
              padding: '8px 14px',
              fontSize: '0.72rem',
              fontWeight: 600,
              color: 'var(--text-muted)',
              textTransform: 'uppercase',
              letterSpacing: '0.06em',
            }}
          >
            Completed
          </div>
          {reports.map((report) => (
            <div
              key={report.id}
              className={`history-item ${activeJobId === report.query_id ? 'active' : ''}`}
              onClick={() => onSelectReport(report.id, report.query_id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter') onSelectReport(report.id, report.query_id);
              }}
            >
              <div className="history-item-query">{report.query_text}</div>
              <div className="history-item-meta">
                <span className="badge badge-status badge-done">✓ Complete</span>
                <span>{formatTime(report.created_at)}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
