'use strict';

require('dotenv').config();
const { Queue, QueueEvents } = require('bullmq');

/**
 * BullMQ queue + event listener for query-time analysis jobs.
 *
 * Queue name: 'query'
 * Consumed by: backend-python worker (app/worker.py)
 *
 * Job data shape (matches python-service-contract.md payload):
 *   { jobId, userId, queryText, sources }
 */
const connection = {
  host: process.env.REDIS_HOST || 'localhost',
  port: parseInt(process.env.REDIS_PORT || '6379'),
};

const queryQueue = new Queue('query', { connection });
const queryQueueEvents = new QueueEvents('query', { connection });

queryQueue.on('error', (err) => {
  console.error('[QueryQueue] BullMQ connection error:', err.message);
});

console.log('[QueryQueue] queryQueue initialised (Redis %s:%s)', connection.host, connection.port);

module.exports = { queryQueue, queryQueueEvents };
