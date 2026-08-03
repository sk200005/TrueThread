# Re-Search Job Queue Contract (BullMQ + Redis)

This document defines the interface between the Node.js API Gateway (producer)
and the Python FastAPI service (consumer/worker) via BullMQ + Redis.

> **Migration note (Aug 2026):** This replaces the previous REST-based contract
> (`POST /api/v1/jobs`, SSE streaming, etc.). The old REST endpoints and
> `pythonServiceClient.js` have been removed.

---

## 1. Architecture Overview

```
┌─────────────┐     BullMQ Queue     ┌─────────────────┐
│  Node.js    │ ───── (Redis) ─────▶ │  Python Worker   │
│  Gateway    │                      │  (FastAPI +      │
│             │ ◀── QueueEvents ──── │   BullMQ Worker) │
│  (Producer) │     (progress,       │  (Consumer)      │
│             │      completed,      │                  │
│             │      failed)         │                  │
└─────────────┘                      └─────────────────┘
```

- **Node** enqueues jobs via `queue.add()` and listens for events via `QueueEvents`.
- **Python** consumes jobs via BullMQ `Worker` and reports progress via `job.updateProgress()`.
- **Redis** is the shared message broker (single instance, AOF persistence).

---

## 2. Queues

| Queue Name   | Purpose                                    | Producer          | Consumer                     |
|------------- |--------------------------------------------|-------------------|------------------------------|
| `research`   | Ingestion pipeline (fetch → store)         | `researchQueue.js` | `worker.py → run_pipeline`   |
| `query`      | Query-time analysis (retrieve → extract → summarize) | `queryQueue.js` | `worker.py → run_query_pipeline` |

---

## 3. Job Data Shape (Producer → Consumer)

When Node enqueues a job, the data payload has this shape:

```json
{
  "jobId": "uuid-v4-string",
  "userId": "uuid-v4-string",
  "queryText": "natural language query string",
  "sources": ["reddit", "youtube"]
}
```

* **Required**: `jobId`, `userId`, `queryText`
* **Optional**: `sources` (defaults to all available if omitted)

### BullMQ Job Options

```javascript
await queue.add('research-job', payload, {
  jobId: payload.jobId,  // Use Postgres UUID as BullMQ job ID
});
```

The Postgres `queries.id` is used as BullMQ's internal job ID so that
`QueueEvents` can correlate progress events back to the originating query.

---

## 4. Progress Events (Consumer → Producer)

The Python worker reports progress via `job.updateProgress(event)` at key
pipeline boundaries. Node listens via `QueueEvents` and forwards these
to the frontend over SSE.

### Event Schema

```json
{
  "type": "progress",
  "jobId": "uuid-v4-string",
  "source": "wikipedia",
  "status": "started",
  "counts": {
    "docsInserted": 5
  },
  "error": "Error message",
  "timestamp": "ISO-8601 string"
}
```

| Field       | Type     | Required | Description                                 |
|------------ |----------|----------|---------------------------------------------|
| `type`      | string   | ✅       | `"connected"` · `"progress"` · `"done"` · `"error"` |
| `jobId`     | string   | ✅       | UUID matching the enqueued job              |
| `source`    | string   | ❌       | Current pipeline node (`"wikipedia"`, `"rag_retrieve"`, etc.) |
| `status`    | string   | ❌       | `"started"` · `"done"` · `"error"`         |
| `counts`    | object   | ❌       | Metric counts (e.g., `docsInserted`, `chunksRetrieved`) |
| `error`     | string   | ❌       | Error message (only when type/status is `"error"`) |
| `timestamp` | string   | ✅       | ISO-8601 UTC timestamp                      |

### Terminal Events

A `type: "done"` or `type: "error"` progress event indicates the job is
fully complete or fatally failed. The Node SSE endpoint closes the client
connection upon receiving a terminal event.

---

## 5. Job Return Value (on completion)

When the worker finishes successfully, it returns a results dict that BullMQ
stores as the job's `returnvalue`:

### Research Queue
```json
{
  "results": {
    "wikipedia": {
      "status": "done",
      "docsInserted": 3,
      "chunksInserted": 12
    }
  },
  "sources_failed": []
}
```

### Query Queue
```json
{
  "report": { ... },
  "chunksRetrieved": 8,
  "claimsExtracted": 5
}
```

---

## 6. SSE Wire Format (Node → Frontend)

The Node gateway (`query.controller.js`) relays BullMQ progress events
to frontend clients over SSE at `GET /api/queries/:jobId/stream`.

Each SSE chunk is formatted as:
```
data: <JSON>\n\n
```

The JSON shape matches the progress event schema above. Terminal events
(`type: "done"` or `type: "error"`) cause the SSE connection to close.

If a client connects after the job has already completed, the Node
controller replays the terminal event from BullMQ's stored job state.

---

## 7. Redis Connection

Both services connect to the same Redis instance:

| Env Var       | Default     | Used By          |
|---------------|-------------|------------------|
| `REDIS_HOST`  | `localhost` | Node + Python    |
| `REDIS_PORT`  | `6379`      | Node + Python    |

Redis is started via `docker-compose up redis` (see `docker-compose.yml`).
AOF persistence is enabled so queued jobs survive Redis restarts.

---

## 8. Status Lifecycle

```
pending → running → done
                  → error (retryable via POST /api/queries/:id/retry)
```

The Python worker updates `queries.status` in Postgres at each transition,
so Node's `GET /api/queries/:id/status` (which reads from Postgres) always
reflects the real state.
