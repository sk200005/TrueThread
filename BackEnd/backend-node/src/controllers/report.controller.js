'use strict';

const db = require('../db/client');

/**
 * GET /api/reports
 * List all reports for the currently authenticated user.
 */
async function listReports(req, res, next) {
  try {
    const userId = req.user.userId;

    const { rows } = await db.query(
      `SELECT r.id, r.query_id, q.query_text, r.sentiment_summary, r.created_at
       FROM reports r
       JOIN queries q ON q.id = r.query_id
       WHERE q.user_id = $1
       ORDER BY r.created_at DESC`,
      [userId]
    );

    return res.json(rows);
  } catch (err) {
    next(err);
  }
}

/**
 * GET /api/reports/:reportId
 * Get a single full report by ID.
 */
async function getReport(req, res, next) {
  try {
    const { reportId } = req.params;
    const userId = req.user.userId;

    const { rows } = await db.query(
      `SELECT r.id, r.query_id, q.query_text, q.sources_requested, q.sources_failed,
              r.sentiment_summary, r.themes, r.verified_claims, r.created_at
       FROM reports r
       JOIN queries q ON q.id = r.query_id
       WHERE r.id = $1 AND q.user_id = $2`,
      [reportId, userId]
    );

    if (!rows.length) {
      return res.status(404).json({ error: 'Report not found' });
    }

    const report = rows[0];

    const rawData = await db.query(
      `SELECT id, source, author, text, url, published_at, engagement_metrics, created_at
       FROM source_documents
       WHERE query_id = $1
       ORDER BY created_at ASC`,
      [report.query_id]
    );
    report.raw_data = rawData.rows;

    return res.json(report);
  } catch (err) {
    next(err);
  }
}

/**
 * DELETE /api/reports/:reportId
 * Delete a report (and cascade-deletes its query and source_documents).
 */
async function deleteReport(req, res, next) {
  try {
    const { reportId } = req.params;
    const userId = req.user.userId;

    // Confirm ownership first
    const { rows } = await db.query(
      `SELECT r.id FROM reports r
       JOIN queries q ON q.id = r.query_id
       WHERE r.id = $1 AND q.user_id = $2`,
      [reportId, userId]
    );

    if (!rows.length) {
      return res.status(404).json({ error: 'Report not found' });
    }

    await db.query('DELETE FROM reports WHERE id = $1', [reportId]);

    return res.status(204).send();
  } catch (err) {
    next(err);
  }
}

module.exports = { listReports, getReport, deleteReport };
