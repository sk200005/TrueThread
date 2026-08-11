import { useState, useCallback } from 'react';
import { getToken, clearToken, logout } from './api/client.js';
import AuthGate from './components/AuthGate.jsx';
import QueryForm from './components/QueryForm.jsx';
import QueryHistory from './components/QueryHistory.jsx';
import LiveStatusPanel from './components/LiveStatusPanel.jsx';
import ReportViewer from './components/ReportViewer.jsx';

/**
 * App — Main shell for the Re-Search frontend.
 *
 * Layout: sidebar (QueryHistory) + main area (QueryForm / LiveStatusPanel / ReportViewer)
 *
 * State machine:
 *   idle      → shows QueryForm
 *   streaming → shows LiveStatusPanel (SSE stream active)
 *   report    → shows ReportViewer
 *
 * No routing library — not needed for this scope since there's no clear
 * use case for shareable job URLs yet. If we add one later, React Router
 * would enable /job/:jobId and /report/:reportId deep links.
 */

// ── View states ──────────────────────────────────────────────────────
// idle: show query form
// streaming: show live status panel for active job
// report: show completed report

export default function App() {
  const [authenticated, setAuthenticated] = useState(!!getToken());
  const [user, setUser] = useState(null);

  // View state machine
  const [view, setView] = useState('idle'); // 'idle' | 'streaming' | 'report'
  const [activeJobId, setActiveJobId] = useState(null);
  const [activeReportId, setActiveReportId] = useState(null);

  // Trigger re-fetch of history when jobs change
  const [historyRefresh, setHistoryRefresh] = useState(0);

  function refreshHistory() {
    setHistoryRefresh((n) => n + 1);
  }

  // ── Auth handlers ──────────────────────────────────────────────────

  function handleAuth(userData) {
    setAuthenticated(true);
    setUser(userData);
  }

  function handleLogout() {
    logout();
    setAuthenticated(false);
    setUser(null);
    setView('idle');
    setActiveJobId(null);
    setActiveReportId(null);
  }

  // ── Navigation handlers ────────────────────────────────────────────

  function handleJobCreated(jobId) {
    setActiveJobId(jobId);
    setActiveReportId(null);
    setView('streaming');
    refreshHistory();
  }

  function handleSelectJob(jobId, queryText, status) {
    setActiveJobId(jobId);
    setActiveReportId(null);

    if (status === 'done') {
      // Job is done but we don't have a reportId from localStorage.
      // The report should be in the reports list; for now, switch to streaming
      // which will replay the terminal event and show the "View Report" button.
      setView('streaming');
    } else if (status === 'error') {
      // Show streaming panel which will display the error with retry button
      setView('streaming');
    } else {
      // In progress — show live stream
      setView('streaming');
    }
  }

  function handleSelectReport(reportId, queryId) {
    setActiveReportId(reportId);
    setActiveJobId(queryId);
    setView('report');
  }

  const handleStreamDone = useCallback(() => {
    refreshHistory();
  }, []);

  function handleViewReport() {
    // After stream completes, switch to report view.
    // We need to find the report for this job. Since reports are 1:1 with queries,
    // the listReports API will have it. For now, we go back to idle and let the
    // user click it from history, OR we could fetch reports and find the match.
    // Better UX: fetch the report list and find the one matching activeJobId.
    setView('report');
    // We set activeReportId to null — ReportViewer will need to look up by query_id.
    // Let's update to support this: we'll use a special mode where we pass queryId
    // instead of reportId.
    setActiveReportId(null);
  }

  function handleBackToForm() {
    setView('idle');
    setActiveJobId(null);
    setActiveReportId(null);
    refreshHistory(); // Reload sidebar so newly completed jobs appear
  }

  // ── Render ─────────────────────────────────────────────────────────

  if (!authenticated) {
    return <AuthGate onAuth={handleAuth} />;
  }

  return (
    <div className="app-layout">
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="logo">
            <div className="logo-icon">🔍</div>
            <span>
              Re<span className="logo-text-highlight">Search</span>
            </span>
          </div>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              marginTop: 12,
            }}
          >
            <button
              className="btn btn-primary btn-sm"
              onClick={handleBackToForm}
              id="new-query-btn"
              style={{ width: '100%' }}
            >
              + New Query
            </button>
          </div>
        </div>

        <div className="sidebar-content">
          <QueryHistory
            activeJobId={activeJobId}
            onSelectJob={handleSelectJob}
            onSelectReport={handleSelectReport}
            refreshTrigger={historyRefresh}
          />
        </div>

        <div
          style={{
            padding: '12px 16px',
            borderTop: '1px solid var(--border-subtle)',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}
        >
          <span style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>
            {user?.email || 'Signed in'}
          </span>
          <button className="btn btn-ghost btn-sm" onClick={handleLogout} id="logout-btn">
            Sign out
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main className="main-content">
        {view === 'idle' && (
          <QueryForm onJobCreated={handleJobCreated} />
        )}

        {view === 'streaming' && activeJobId && (
          <LiveStatusPanel
            key={activeJobId}
            jobId={activeJobId}
            onDone={handleStreamDone}
            onViewReport={handleViewReport}
          />
        )}

        {view === 'report' && (
          <ReportViewerWithLookup
            reportId={activeReportId}
            queryId={activeJobId}
            onBack={handleBackToForm}
          />
        )}
      </main>
    </div>
  );
}

/**
 * Wrapper that handles two cases:
 *   1. We have a reportId → fetch directly
 *   2. We only have a queryId → list reports, find the matching one
 */
import { useState as _useState, useEffect as _useEffect } from 'react';
import { listReports as _listReports } from './api/client.js';

function ReportViewerWithLookup({ reportId, queryId, onBack }) {
  const [resolvedReportId, setResolvedReportId] = _useState(reportId);
  const [looking, setLooking] = _useState(!reportId && !!queryId);

  _useEffect(() => {
    if (reportId) {
      setResolvedReportId(reportId);
      return;
    }
    if (!queryId) return;

    let cancelled = false;
    // The Python worker saves the report to DB just before emitting the
    // SSE 'done' event. There can be a small race where the frontend
    // arrives here before the DB write commits. Retry up to 5 times
    // with a 1 s delay to handle this gracefully.
    async function lookup() {
      setLooking(true);
      const MAX_RETRIES = 5;
      const RETRY_DELAY_MS = 1000;
      for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
        if (cancelled) break;
        try {
          const reports = await _listReports();
          const match = reports.find((r) => r.query_id === queryId);
          if (match) {
            if (!cancelled) setResolvedReportId(match.id);
            break; // Found it — stop retrying
          }
        } catch {
          // Network error — still retry
        }
        if (attempt < MAX_RETRIES - 1) {
          // Wait before next retry
          await new Promise((resolve) => setTimeout(resolve, RETRY_DELAY_MS));
        }
      }
      if (!cancelled) setLooking(false);
    }
    lookup();
    return () => { cancelled = true; };
  }, [reportId, queryId]);

  if (looking) {
    return (
      <div className="empty-state animate-fade-in">
        <span className="spinner" style={{ width: 28, height: 28 }} />
        <div className="empty-state-title" style={{ marginTop: 16 }}>
          Loading report…
        </div>
      </div>
    );
  }

  if (!resolvedReportId) {
    return (
      <div className="glass-card animate-fade-in" style={{ textAlign: 'center' }}>
        <div style={{ fontSize: '2rem', marginBottom: 8 }}>📋</div>
        <h3>Report not ready</h3>
        <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: 16 }}>
          The report for this job hasn&apos;t been generated yet. Try again after the pipeline completes.
        </p>
        <button className="btn btn-ghost" onClick={onBack}>
          ← Back
        </button>
      </div>
    );
  }

  return <ReportViewer reportId={resolvedReportId} onBack={onBack} />;
}
