'use strict';

const express = require('express');
const { authenticate } = require('../middleware/authenticate');
const {
  submitQuery,
  getJobStatus,
  streamJobProgress,
  retryJob,
  stopJob,
  chatWithQuery,
} = require('../controllers/query.controller');

const router = express.Router();

// POST /api/queries — submit a new research query (requires auth)
router.post('/', authenticate, submitQuery);

// GET /api/queries/:jobId/status — poll job status (requires auth)
router.get('/:jobId/status', authenticate, getJobStatus);

// GET /api/queries/:jobId/stream — SSE stream of live progress events (requires auth)
router.get('/:jobId/stream', authenticate, streamJobProgress);

// POST /api/queries/:jobId/retry — re-enqueue a failed job from its last checkpoint
router.post('/:jobId/retry', authenticate, retryJob);

// POST /api/queries/:jobId/stop — cancel a job
router.post('/:jobId/stop', authenticate, stopJob);

// POST /api/queries/:jobId/chat — RAG Chatbot
router.post('/:jobId/chat', authenticate, chatWithQuery);

module.exports = router;
