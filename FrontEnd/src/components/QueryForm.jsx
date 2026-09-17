import { useState } from 'react';
import { submitQuery } from '../api/client.js';
import { addJob } from '../api/jobHistory.js';

/**
 * QueryForm — Submit a new research query.
 *
 * POSTs to POST /api/queries with { queryText, sources? }.
 * On success, stores the jobId in localStorage history and
 * hands off to the parent via onJobCreated(jobId).
 *
 * Available sources match the backend's allowed values:
 * reddit, youtube, wikipedia.
 */

const AVAILABLE_SOURCES = [
  { id: 'reddit', label: 'Reddit', icon: '💬' },
  { id: 'youtube', label: 'YouTube', icon: '📺' },
  { id: 'wikipedia', label: 'Wikipedia', icon: '📚' },
];

export default function QueryForm({ onJobCreated }) {
  // HLD: State Management
  // Maintains local UI state for the research query input (queryText), user-selected data sources (selectedSources),
  // as well as UI feedback states during network requests (loading indicator and error messages).
  const [queryText, setQueryText] = useState('');
  const [selectedSources, setSelectedSources] = useState(['reddit', 'youtube', 'wikipedia']);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // HLD: Source Selection Logic
  // Acts as a toggler for the research sources. It ensures at least one source can be selected
  // and dynamically updates the selectedSources array, which dictates where the backend will scrape data from.
  function toggleSource(sourceId) {
    setSelectedSources((prev) =>
      prev.includes(sourceId)
        ? prev.filter((s) => s !== sourceId)
        : [...prev, sourceId]
    );
  }

  // HLD: API Interaction & Data Flow
  // 1. Validates user input before proceeding.
  // 2. Triggers the backend API (`submitQuery`) to initiate a new async research task.
  // 3. Persists the returned job metadata (like jobId) locally via `addJob` so it survives page reloads.
  // 4. Emits an event (`onJobCreated`) back up to the parent component (App.jsx) to transition the UI 
  //    from the input form to the LiveStatus monitoring view.
  async function handleSubmit(e) {
    e.preventDefault();
    if (!queryText.trim()) return;

    setError('');
    setLoading(true);

    try {
      const result = await submitQuery(queryText.trim(), selectedSources);
      // result: { jobId, status: 'pending', query, message }

      // Track in localStorage
      addJob(result.jobId, queryText.trim(), selectedSources);

      // Hand off to parent
      onJobCreated(result.jobId, queryText.trim());

      // Clear form
      setQueryText('');
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  // HLD: UI Rendering & User Interaction
  // Renders a controlled form component consisting of a text area for the query, 
  // dynamic checkbox buttons for available sources, and a submit button that handles 
  // loading states and form validation to prevent empty submissions.
  return (
    <div className="animate-fade-in">
      <div className="glass-card" style={{ marginBottom: 24 }}>
        <h2 style={{ marginBottom: 4 }}>New Research Query</h2>
        <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: 20 }}>
          Enter a topic or question to research across multiple sources.
        </p>

        <form onSubmit={handleSubmit}>
          <div className="form-group" style={{ marginBottom: 16 }}>
            <label className="form-label" htmlFor="query-text">
              Research Query
            </label>
            <textarea
              id="query-text"
              className="textarea"
              placeholder="e.g., What are people saying about the new iPhone battery life?"
              value={queryText}
              onChange={(e) => setQueryText(e.target.value)}
              rows={4}
              required
              disabled={loading}
            />
          </div>

          <div className="form-group" style={{ marginBottom: 20 }}>
            <label className="form-label">Sources</label>
            <div className="source-checkboxes">
              {AVAILABLE_SOURCES.map((src) => (
                <label
                  key={src.id}
                  className={`source-checkbox ${selectedSources.includes(src.id) ? 'selected' : ''}`}
                >
                  <input
                    type="checkbox"
                    checked={selectedSources.includes(src.id)}
                    onChange={() => toggleSource(src.id)}
                    disabled={loading}
                  />
                  <span>{src.icon}</span>
                  <span>{src.label}</span>
                </label>
              ))}
            </div>
          </div>

          {error && (
            <div className="auth-error" style={{ marginBottom: 14 }}>
              {error}
            </div>
          )}

          <button
            id="submit-query"
            className="btn btn-primary"
            type="submit"
            disabled={loading || !queryText.trim() || selectedSources.length === 0}
            style={{ width: '100%' }}
          >
            {loading ? (
              <>
                <span className="spinner" />
                Submitting…
              </>
            ) : (
              <>🚀 Start Research</>
            )}
          </button>
        </form>
      </div>
    </div>
  );
}
