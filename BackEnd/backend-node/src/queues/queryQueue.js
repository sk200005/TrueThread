'use strict';

require('dotenv').config();
const { Queue, QueueEvents } = require('bullmq');   //imports two classes from BullMQ.

/**
 * BullMQ queue + event listener for query-time analysis jobs.
 *
 * Queue name: 'query'
 * Consumed by: backend-python worker (app/worker.py)
 *
 * Job data shape (matches python-service-contract.md payload):
 *   { jobId, userId, queryText, sources }
 */

//Queue -->  Use : Creates a queue and Add Job to it.
//QueueEvents --> Use : To listen for events happening in the queue.
//                      It does not process jobs, only observes what is happening.

const connection = {
  host: process.env.REDIS_HOST || 'localhost',
  port: parseInt(process.env.REDIS_PORT || '6379'),
};

const queryQueue = new Queue('query', { connection });            //Just creates (or connects if already exists) a queue named      
const queryQueueEvents = new QueueEvents('query', { connection }); // This connects an event listener to the same queue. This is used to track
                                                                   
                                                                   
queryQueue.on('error', (err) => {
  console.error('[QueryQueue] BullMQ connection error:', err.message);
});

console.log('[QueryQueue] queryQueue initialised (Redis %s:%s)', connection.host, connection.port);

module.exports = { queryQueue, queryQueueEvents };
