'use strict';

require('dotenv').config();
const { Queue, QueueEvents } = require('bullmq');

/**
 * BullMQ queue + event listener for research/ingestion jobs.
 *
 * Queue name: 'research'
 * Consumed by: backend-python worker (app/worker.py)
 *
 * Job data shape (matches python-service-contract.md payload):
 *   { jobId, userId, queryText, sources }
 */
const connection = {
  host: process.env.REDIS_HOST || 'localhost',
  port: parseInt(process.env.REDIS_PORT || '6379'),
};

const researchQueue = new Queue('research', { connection });
const researchQueueEvents = new QueueEvents('research', { connection });

researchQueue.on('error', (err) => {
  console.error('[ResearchQueue] BullMQ connection error:', err.message);
});

console.log('[ResearchQueue] researchQueue initialised (Redis %s:%s)', connection.host, connection.port);

module.exports = { researchQueue, researchQueueEvents };
