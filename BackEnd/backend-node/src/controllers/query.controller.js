'use strict';

/**
 * query.controller.js
 *
 * Handles research query lifecycle:
 *   POST /api/queries        — create job, submit to Python FastAPI service
 *   GET  /api/queries/:id/status  — poll DB for current job status
 *   GET  /api/queries/:id/stream  — SSE: relay live progress events from Python service
 *   POST /api/queries/:id/retry   — re-submit a failed job to Python service
 *
 * SSE Bridge:
 *   This controller connects to the Python service's SSE endpoint and relays
 *   events to the frontend client. Node acts as a pure proxy — no scraping logic.
 *
 * MIGRATION NOTE (July 2026):
 *   Job execution moved from researchWorker.js (in-memory queue) to backend-python
 *   (FastAPI + LangGraph). The in-memory worker is retained but unused — see
 *   researchWorker.js for rollback instructions.
 */

const db = require('../db/client');
const { submitJob, streamJobProgress: streamFromPython } = require('../services/pythonServiceClient');

// @deprecated — retained for emergency rollback only. See researchWorker.js header.
// const { addJob, jobEvents } = require('../workers/researchWorker');

/**
 * POST /api/queries
 * Validates input, writes a `queries` row, submits to Python service, returns jobId immediately.
 */
async function submitQuery(req, res, next) {
  try {
    const { queryText, sources } = req.body;
    const userId = req.user.userId;

    if (!queryText || typeof queryText !== 'string' || !queryText.trim()) {
      return res.status(400).json({ error: 'queryText is required' });
    }

    const sourcesRequested = Array.isArray(sources)
      ? sources
      : ['reddit', 'youtube'];

    // 1. Persist the query row — status starts as 'pending'
    const { rows } = await db.query(
      `INSERT INTO queries (user_id, query_text, status, sources_requested)
       VALUES ($1, $2, 'pending', $3)
       RETURNING id, query_text, status, created_at`,
      [userId, queryText.trim(), sourcesRequested]
    );
    const jobId = rows[0].id;

    // 2. Submit job to Python FastAPI service (non-blocking from our perspective —
    //    Python runs the LangGraph pipeline as a background task)
    try {
      await submitJob({
        jobId,
        userId,
        queryText: queryText.trim(),
        sources: sourcesRequested,
      });
    } catch (pythonErr) {
      // If Python service is down, mark job as error immediately
      await db.query(
        "UPDATE queries SET status = 'error' WHERE id = $1",
        [jobId]
      );
      return res.status(502).json({
        error: 'Failed to reach Python pipeline service',
        jobId,
      });
    }

    return res.status(202).json({
      jobId,
      status: 'pending',
      query: queryText.trim(),
      message: 'Job accepted. Poll /status or connect to /stream for progress.',
    });
  } catch (err) {
    next(err);
  }
}

/**
 * GET /api/queries/:jobId/status
 * Simple polling endpoint — returns the current row from the queries table.
 */
async function getJobStatus(req, res, next) {
  try {
    const { jobId } = req.params;
    const userId = req.user.userId;

    const { rows } = await db.query(
      `SELECT id, query_text, status, sources_requested, sources_failed, created_at, completed_at
       FROM queries
       WHERE id = $1 AND user_id = $2`,
      [jobId, userId]
    );

    if (!rows.length) {
      return res.status(404).json({ error: 'Job not found' });
    }

    return res.json(rows[0]);
  } catch (err) {
    next(err);
  }
}

/**
 * GET /api/queries/:jobId/stream
 *
 * SSE relay — connects to the Python service's SSE endpoint and forwards
 * all events to the frontend client. Node adds no events of its own.
 *
 * Event format (from Python): { type, jobId, source?, status?, counts?, error?, timestamp }
 *
 * The connection closes automatically when Python emits type='done' or type='error'.
 */

// Job of this function  -listen to progress updates from the Python server and forward 
// them to the frontend using Server-Sent Events (SSE).

function streamJobProgress(req, res) {
  const { jobId } = req.params;

  // SSE response headers
  // sent from the server to the browser
  res.set({
    'Content-Type':  'text/event-stream',   
    'Cache-Control': 'no-cache',            // Never cache live updates
    'Connection':    'keep-alive',          // Don't close socket
    'X-Accel-Buffering': 'no',              // Disable Nginx buffering if behind a proxy
  });
  res.flushHeaders();

  // streamFromPython(jobId) connect to Python service's SSE stream and relay events like : 
      // {
      //     "type":"progress",
      //     "status":"Fetching Wikipedia"
      // }

  const pythonStream = streamFromPython(jobId);

  pythonStream.on('event', (event) => {
    sendEvent(res, event);
    // Auto-close SSE stream when the job reaches a terminal state
    if (event.type === 'done' || event.type === 'error') {
      res.end();
    }
  });

  pythonStream.on('error', (err) => {
    // Python service unreachable — send error event and close
    sendEvent(res, {
      type: 'error',
      jobId,
      status: 'error',
      error: 'Lost connection to pipeline service',
      timestamp: new Date().toISOString(),
    });
    res.end();
  });

  pythonStream.on('end', () => {
    res.end();
  });

  // Clean up when the client disconnects early
  req.on('close', () => {
    // pythonStream will be GC'd — no explicit cleanup needed for the EventEmitter
  });
}

/**
 * POST /api/queries/:jobId/retry
 *
 * Re-submits a failed job to the Python service.
 * Only jobs with status='error' can be retried.
 */
/**Frontend
     │
     │ POST /api/queries/101/retry
     ▼
retryJob()
     │
     ├── Check if job exists
     ├── Verify the logged-in user owns it
     ├── Verify job status is "error"
     ├── Reset database status to "pending"
     ├── Decide which sources to retry
     ├── Send job to Python
     └── Return "Job accepted" */
async function retryJob(req, res, next) {
  try {
    const { jobId } = req.params;
    const userId = req.user.userId;

    // Verify ownership and current status
    const { rows } = await db.query(
      `SELECT id, query_text, status, sources_requested, sources_failed
       FROM queries WHERE id = $1 AND user_id = $2`,
      [jobId, userId]
    );

    if (!rows.length) {                                         //Check if job exists 
      return res.status(404).json({ error: 'Job not found' });
    }

    const job = rows[0];
    if (job.status !== 'error') {                               //Verify job status is "error"
      return res.status(409).json({
        error: `Job cannot be retried in status '${job.status}'. Only 'error' jobs can be retried.`,
      });
    }

                      
    await db.query(                                            // Reset database status to "pending" && clear sources_failed
      "UPDATE queries SET status = 'pending', sources_failed = NULL, completed_at = NULL WHERE id = $1",
      [jobId]
    );

    // Decide which sources to retry
    const sourcesToRetry = (job.sources_failed && job.sources_failed.length > 0)
      ? job.sources_failed
      : job.sources_requested;

    // Send job to Python
    try {
      await submitJob({
        jobId,
        userId,
        queryText: job.query_text,
        sources: sourcesToRetry,
      });
    } catch (pythonErr) {
      await db.query(
        "UPDATE queries SET status = 'error' WHERE id = $1",
        [jobId]
      );
      return res.status(502).json({
        error: 'Failed to reach Python pipeline service',
        jobId,
      });
    }

    return res.status(202).json({
      jobId,
      status: 'pending',
      message: 'Job re-enqueued. It will resume from the last checkpoint.',
    });
  } catch (err) {
    next(err);
  }
}

// ── SSE helper ────────────────────────────────────────────────────────────────

function sendEvent(res, data) {
  res.write(`data: ${JSON.stringify(data)}\n\n`);
}

module.exports = { submitQuery, getJobStatus, streamJobProgress, retryJob };
