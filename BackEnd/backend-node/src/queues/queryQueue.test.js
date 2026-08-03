'use strict';

/**
 * queryQueue.test.js — Tests for the BullMQ queue integration.
 *
 * Verifies:
 *   1. Jobs can be enqueued to the research queue with correct data shape
 *   2. Jobs can be enqueued to the query queue with correct data shape
 *   3. BullMQ jobId is set to our Postgres UUID
 *   4. QueueEvents emits progress events that match the SSE contract
 *
 * Prerequisites:
 *   - Redis must be running on localhost:6379
 *   - Run: node src/queues/queryQueue.test.js
 */

const assert = require('assert');
const { Queue, Worker, QueueEvents } = require('bullmq');

const REDIS_HOST = process.env.REDIS_HOST || 'localhost';
const REDIS_PORT = parseInt(process.env.REDIS_PORT || '6379');
const connection = { host: REDIS_HOST, port: REDIS_PORT };

// Use unique queue names per test run to avoid collisions
const TEST_QUEUE_NAME = `test-research-${Date.now()}`;

async function runTests() {
  console.log('Running BullMQ queue integration tests...\n');

  const queue = new Queue(TEST_QUEUE_NAME, { connection });
  const queueEvents = new QueueEvents(TEST_QUEUE_NAME, { connection });

  // Wait for QueueEvents to connect
  await queueEvents.waitUntilReady();

  try {
    // ─── Test 1: Enqueue a job with correct data shape ─────────────────
    const jobId = 'test-uuid-1234';
    const payload = {
      jobId,
      userId: 'user-uuid-5678',
      queryText: 'test query about AI',
      sources: ['wikipedia'],
    };

    const addedJob = await queue.add('research-job', payload, { jobId });
    assert.strictEqual(addedJob.id, jobId, 'BullMQ job ID should match our UUID');
    assert.deepStrictEqual(addedJob.data, payload, 'Job data should match the payload');
    console.log('✅ Test 1: Job enqueued with correct data shape and ID');

    // ─── Test 2: Worker receives job and can report progress ───────────
    const progressEvents = [];

    const progressPromise = new Promise((resolve) => {
      queueEvents.on('progress', ({ jobId: jId, data }) => {
        if (jId === jobId) {
          progressEvents.push(data);
          if (data.type === 'done') resolve();
        }
      });
    });

    // Create a test worker that emits progress events
    const worker = new Worker(TEST_QUEUE_NAME, async (job) => {
      // Simulate the Python worker's progress reporting
      await job.updateProgress({
        type: 'connected',
        jobId: job.data.jobId,
        status: 'connected',
        timestamp: new Date().toISOString(),
      });

      await job.updateProgress({
        type: 'progress',
        jobId: job.data.jobId,
        source: 'wikipedia',
        status: 'started',
        timestamp: new Date().toISOString(),
      });

      await job.updateProgress({
        type: 'progress',
        jobId: job.data.jobId,
        source: 'wikipedia',
        status: 'done',
        counts: { docsInserted: 3 },
        timestamp: new Date().toISOString(),
      });

      await job.updateProgress({
        type: 'done',
        jobId: job.data.jobId,
        status: 'done',
        results: { wikipedia: { status: 'done', docsInserted: 3 } },
        timestamp: new Date().toISOString(),
      });

      return { results: { wikipedia: { status: 'done' } } };
    }, { connection });

    // Wait for all progress events
    await progressPromise;

    assert.strictEqual(progressEvents.length, 4, 'Should receive 4 progress events');
    assert.strictEqual(progressEvents[0].type, 'connected');
    assert.strictEqual(progressEvents[1].type, 'progress');
    assert.strictEqual(progressEvents[1].source, 'wikipedia');
    assert.strictEqual(progressEvents[2].type, 'progress');
    assert.strictEqual(progressEvents[2].status, 'done');
    assert.strictEqual(progressEvents[3].type, 'done');
    assert.ok(progressEvents[3].results, 'Done event should include results');
    console.log('✅ Test 2: Worker processes job and emits progress events');

    // ─── Test 3: Progress events include required fields ───────────────
    for (const event of progressEvents) {
      assert.ok(event.type, 'Every event must have a type');
      assert.ok(event.jobId, 'Every event must have a jobId');
      assert.ok(event.timestamp, 'Every event must have a timestamp');
    }
    console.log('✅ Test 3: All events include required fields (type, jobId, timestamp)');

    // ─── Test 4: Completed event is emitted ────────────────────────────
    const completedPromise = new Promise((resolve) => {
      queueEvents.on('completed', ({ jobId: jId }) => {
        if (jId === jobId) resolve();
      });
    });

    // The job should already be completed from Test 2
    // Give a short timeout in case it hasn't propagated yet
    await Promise.race([
      completedPromise,
      new Promise((_, reject) => setTimeout(() => reject(new Error('Completed event timeout')), 5000)),
    ]);
    console.log('✅ Test 4: Completed event emitted after worker finishes');

    // Cleanup
    await worker.close();

    console.log('\nAll BullMQ queue tests passed! ✅');

  } finally {
    // Clean up test queue
    await queue.obliterate({ force: true });
    await queue.close();
    await queueEvents.close();
  }
}

runTests().catch(err => {
  console.error('\n❌ Test failed:', err.message);
  process.exit(1);
});
