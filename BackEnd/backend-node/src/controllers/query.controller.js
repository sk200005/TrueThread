'use strict';

/**
 * query.controller.js
 *
 * Handles research query lifecycle:
 *   POST /api/queries        — create job, enqueue to Redis via BullMQ
 *   GET  /api/queries/:id/status  — poll DB for current job status
 *   GET  /api/queries/:id/stream  — SSE: forward BullMQ progress events to client
 *   POST /api/queries/:id/retry   — re-enqueue a failed job via BullMQ
 *
 * SSE Bridge:
 *   This controller listens to BullMQ QueueEvents (progress/completed/failed)
 *   on both the 'research' and 'query' queues and relays matching events to
 *   the frontend client over SSE. Node acts as a pure relay — no scraping logic.
 *
 * MIGRATION NOTE (Aug 2026):
 *   Job dispatch uses BullMQ + Redis. Python workers consume jobs
 *   directly from Redis queues ('research' and 'query').
 */

const { Job, FlowProducer } = require('bullmq');
const db = require('../db/client');
const { researchQueue, researchQueueEvents } = require('../queues/researchQueue');
const { queryQueue, queryQueueEvents } = require('../queues/queryQueue');

const flowProducer = new FlowProducer({ connection: researchQueue.opts.connection });

/**
 * POST /api/queries
 * Validates input, writes a `queries` row, enqueues to BullMQ, returns jobId immediately.
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

    // 2. Enqueue the jobs to Redis via BullMQ using FlowProducer.
    //    query-job depends on research-job.
    try {
      await flowProducer.add({
        name: 'query-job',
        queueName: 'query',
        data: {
          jobId,
          userId,
          queryText: queryText.trim(),
          sources: sourcesRequested,
        },
        opts: { jobId },
        children: [
          {
            name: 'research-job',
            queueName: 'research',
            data: {
              jobId,
              userId,
              queryText: queryText.trim(),
              sources: sourcesRequested,
            },
            opts: { jobId },
          }
        ]
      });
    } catch (redisErr) {
      // If Redis is down, mark job as error immediately
      await db.query(
        "UPDATE queries SET status = 'error', error_message = 'Failed to enqueue job (Redis unavailable)' WHERE id = $1",
        [jobId]
      );
      return res.status(502).json({
        error: 'Failed to enqueue job (Redis unavailable)',
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
      `SELECT id, query_text, status, sources_requested, sources_failed, error_message, created_at, completed_at
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
 * SSE endpoint — listens to BullMQ QueueEvents for progress/completed/failed
 * and forwards matching events to the frontend client.
 *
 * Event format (unchanged from previous REST-based contract):
 *   { type, jobId, source?, status?, counts?, error?, timestamp }
 *
 * Terminal events: type="done" or type="error" close the SSE connection.
 *
 * If the job already completed/failed before the client connects, the
 * terminal event is replayed immediately.
 */
async function streamJobProgress(req, res) {
  const { jobId } = req.params;

  // SSE response headers
  res.set({
    'Content-Type':  'text/event-stream',
    'Cache-Control': 'no-cache',            // Never cache live updates
    'Connection':    'keep-alive',          // Don't close socket
    'X-Accel-Buffering': 'no',              // Disable Nginx buffering if behind a proxy
  });
  res.flushHeaders();

  let closed = false;

  // ── Check if job already completed/failed before SSE connected ────────
  try {
    let existingJob = await Job.fromId(researchQueue, jobId);
    if (!existingJob) existingJob = await Job.fromId(queryQueue, jobId);

    if (existingJob) {
      const state = await existingJob.getState();

      if (state === 'completed') {
        const results = existingJob.returnvalue || {};
        sendEvent(res, {
          type: 'done', jobId, status: 'done', results,
          timestamp: new Date().toISOString(),
        });
        res.end();
        return;
      }

      if (state === 'failed') {
        sendEvent(res, {
          type: 'error', jobId, status: 'error',
          error: existingJob.failedReason,
          timestamp: new Date().toISOString(),
        });
        res.end();
        return;
      }
    }
  } catch (lookupErr) {
    // Redis might be down — fall through to live listener, which will
    // reconnect automatically when Redis comes back.
  }

  // ── Live event handlers ───────────────────────────────────────────────
  // BullMQ QueueEvents are queue-wide; we filter by jobId.

  function handleProgress({ jobId: jId, data }) {
    if (closed || jId !== jobId) return;
    const event = (typeof data === 'string') ? JSON.parse(data) : data;
    sendEvent(res, event);
    // The worker sends terminal events via updateProgress too
    if (event.type === 'done' || event.type === 'error') {
      close();
    }
  }

  function handleCompleted({ jobId: jId, returnvalue }) {
    if (closed || jId !== jobId) return;
    // Fallback — the worker should have already sent a 'done' progress
    // event, but if the client missed it, handle the BullMQ completed event.
    const results = returnvalue
      ? (typeof returnvalue === 'string' ? JSON.parse(returnvalue) : returnvalue)
      : {};
    sendEvent(res, {
      type: 'done', jobId, status: 'done', results,
      timestamp: new Date().toISOString(),
    });
    close();
  }

  function handleFailed({ jobId: jId, failedReason }) {
    if (closed || jId !== jobId) return;
    sendEvent(res, {
      type: 'error', jobId, status: 'error',
      error: failedReason,
      timestamp: new Date().toISOString(),
    });
    close();
  }

  // Listen on both queues — jobIds are globally unique UUIDs, so no conflict.
  researchQueueEvents.on('progress', handleProgress);
  researchQueueEvents.on('failed', handleFailed);
  queryQueueEvents.on('progress', handleProgress);
  queryQueueEvents.on('completed', handleCompleted);
  queryQueueEvents.on('failed', handleFailed);

  // Keepalive every 30s to prevent proxy/client timeout
  const keepalive = setInterval(() => {
    if (!closed) res.write(': keepalive\n\n');
  }, 30000);

  function close() {
    if (closed) return;
    closed = true;
    researchQueueEvents.off('progress', handleProgress);
    researchQueueEvents.off('failed', handleFailed);
    queryQueueEvents.off('progress', handleProgress);
    queryQueueEvents.off('completed', handleCompleted);
    queryQueueEvents.off('failed', handleFailed);
    clearInterval(keepalive);
    res.end();
  }

  // Clean up when the client disconnects early
  req.on('close', close);
}

/**
 * POST /api/queries/:jobId/retry
 *
 * Re-enqueues a failed job via BullMQ.
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
     ├── Re-enqueue to Redis via BullMQ
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
      "UPDATE queries SET status = 'pending', sources_failed = NULL, error_message = NULL, completed_at = NULL WHERE id = $1",
      [jobId]
    );

    // Decide which sources to retry
    const sourcesToRetry = (job.sources_failed && job.sources_failed.length > 0)
      ? job.sources_failed
      : job.sources_requested;

    // Re-enqueue to Redis via BullMQ
    try {
      // Remove the old failed jobs from Redis so we can reuse the same jobId
      const oldResearchJob = await Job.fromId(researchQueue, jobId);
      if (oldResearchJob) await oldResearchJob.remove();
      const oldQueryJob = await Job.fromId(queryQueue, jobId);
      if (oldQueryJob) await oldQueryJob.remove();

      await flowProducer.add({
        name: 'query-job',
        queueName: 'query',
        data: {
          jobId,
          userId,
          queryText: job.query_text,
          sources: sourcesToRetry,
        },
        opts: { jobId },
        children: [
          {
            name: 'research-job',
            queueName: 'research',
            data: {
              jobId,
              userId,
              queryText: job.query_text,
              sources: sourcesToRetry,
            },
            opts: { jobId },
          }
        ]
      });
    } catch (redisErr) {
      await db.query(
        "UPDATE queries SET status = 'error', error_message = 'Failed to enqueue job (Redis unavailable)' WHERE id = $1",
        [jobId]
      );
      return res.status(502).json({
        error: 'Failed to enqueue job (Redis unavailable)',
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

/**
 * POST /api/queries/:jobId/stop
 * Cancels a job and updates the status to 'cancelled'
 */
async function stopJob(req, res, next) {
  try {
    const { jobId } = req.params;
    const userId = req.user.userId;

    // Verify ownership
    const { rows } = await db.query(
      `SELECT id, status FROM queries WHERE id = $1 AND user_id = $2`,
      [jobId, userId]
    );

    if (!rows.length) {
      return res.status(404).json({ error: 'Job not found' });
    }

    const job = rows[0];
    if (job.status === 'done' || job.status === 'error' || job.status === 'cancelled') {
      return res.status(400).json({ error: 'Job is already finished or cancelled.' });
    }

    // Update DB
    await db.query(
      "UPDATE queries SET status = 'cancelled' WHERE id = $1",
      [jobId]
    );

    // Try to remove from BullMQ (ignore errors if it's already active/removed)
    try {
      let bJob = await Job.fromId(researchQueue, jobId);
      if (bJob) await bJob.remove();
      bJob = await Job.fromId(queryQueue, jobId);
      if (bJob) await bJob.remove();
    } catch (err) {
      // Ignored: Job might be active and BullMQ throws when removing active jobs
    }

    return res.status(200).json({ jobId, status: 'cancelled', message: 'Job cancelled' });
  } catch (err) {
    next(err);
  }
}

// ── SSE helper ────────────────────────────────────────────────────────────────

function sendEvent(res, data) {
  res.write(`data: ${JSON.stringify(data)}\n\n`);
}

module.exports = { submitQuery, getJobStatus, streamJobProgress, retryJob, stopJob };
