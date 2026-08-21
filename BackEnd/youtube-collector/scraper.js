'use strict';

/**
 * scraper.js — Hardened YouTube scraper (importable module).
 *
 * Mirrors reddit-collector/scraper.js structure exactly.
 * Upgraded with:
 *   - Retry with exponential backoff on every API call
 *   - Checkpoint: saves last processed videoId so retries resume mid-scrape
 *   - Structured JSON logging for every step
 *
 * Exports:
 *   scrape(jobId, query, queryId) → Promise<{ videosScraped, docsInserted, skipped }>
 */

require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const path = require('path');
const logger = require('../shared/logger');
const { withRetry } = require('../shared/retry');
const { saveCheckpoint, loadCheckpoint, clearCheckpoint } = require('../shared/checkpoint');
const { saveToPostgres } = require('./db-helper');

// Re-use existing src/ modules — no duplication
const { searchVideos }                     = require('./src/search');
const { getVideoMetadata }                 = require('./src/metadata');
const { getTranscript }                    = require('./src/transcript');
const { cleanTranscript, cleanDescription }= require('./src/cleaner');
const { translateTranscript }              = require('./src/translator');
const { getTopComments }                   = require('./src/comments');

const TARGET_COUNT = 5;

/**
 * scrape — Run the full YouTube collection pipeline for a query.
 *
 * @param {string}      jobId   — UUID from queries.id (null for standalone CLI runs)
 * @param {string}      query   — The search query
 * @param {string|null} queryId — Alias for jobId (db-helper compatibility)
 * @returns {Promise<{ videosScraped: number, docsInserted: number, skipped: number }>}
 */
async function scrape(jobId, query, queryId = null) {
  const source = 'youtube';
  const startTime = Date.now();
  const apiKey = process.env.YOUTUBE_API_KEY;

  if (!apiKey) {
    throw new Error('YOUTUBE_API_KEY is not set in environment variables');
  }

  logger.info({ jobId, source, status: 'started', message: 'YouTube scrape started', query });

  // Load checkpoint — returns null if this is a fresh job
  const lastProcessedId = await loadCheckpoint(jobId, source);
  if (lastProcessedId) {
    logger.info({ jobId, source, message: `Resuming from checkpoint. Last processed videoId: ${lastProcessedId}` });
  }

  let videosScraped = 0;
  let docsInserted = 0;
  let skipped = 0;
  let foundCheckpoint = !lastProcessedId;

  try {
    // 1. Search YouTube — wrap in retry for transient network errors
    const videoIds = await withRetry(
      () => searchVideos(query, apiKey),
      { maxRetries: 3, baseDelayMs: 1000, label: 'youtube-search' }
    );

    logger.info({ jobId, source, message: `Found ${videoIds.length} candidate video IDs` });

    // 2. Fetch batch metadata — one API call for all IDs
    const rankedVideos = await withRetry(
      () => getVideoMetadata(videoIds, apiKey),
      { maxRetries: 3, baseDelayMs: 1000, label: 'youtube-metadata' }
    );

    logger.info({ jobId, source, message: `Processing ${Math.min(rankedVideos.length, TARGET_COUNT)} videos` });

    // 3. Process each video
    for (const video of rankedVideos) {
      if (videosScraped >= TARGET_COUNT) break;

      try {
        // Checkpoint resume: skip videos already processed in a previous run
        if (!foundCheckpoint) {
          if (video.videoId === lastProcessedId) {
            foundCheckpoint = true; // Found the checkpoint — next video is new
          }
          logger.debug({ jobId, source, message: `Skipping already-processed video`, videoId: video.videoId });
          skipped++;
          continue;
        }

        logger.info({ jobId, source, message: `Processing video`, videoId: video.videoId, title: video.title });

        // 4. Fetch transcript — mandatory; skip video if unavailable
        const rawTranscript = await withRetry(
          () => getTranscript(video.videoId),
          { maxRetries: 2, baseDelayMs: 500, label: `youtube-transcript:${video.videoId}` }
        );

        if (!rawTranscript) {
          logger.warn({ jobId, source, message: 'No English transcript available, skipping video', videoId: video.videoId });
          skipped++;
          continue;
        }

        // 5. Clean + translate transcript
        const cleanedTranscript = cleanTranscript(rawTranscript.text);
        const cleanedDescription = cleanDescription(video.description);
        const transcriptObj = await translateTranscript(cleanedTranscript);

        // 6. Fetch top comments — failure is non-fatal (videos can have comments disabled)
        let comments = [];
        try {
          comments = await withRetry(
            () => getTopComments(video.videoId, apiKey, 5),
            { maxRetries: 2, baseDelayMs: 500, label: `youtube-comments:${video.videoId}` }
          );
        } catch (commentErr) {
          logger.warn({ jobId, source, message: 'Failed to fetch comments', videoId: video.videoId, error: commentErr });
        }

        const videoRecord = {
          videoId:     video.videoId,
          title:       video.title,
          channel:     video.channel,
          publishedAt: video.publishedAt,
          views:       video.views,
          likes:       video.likes,
          duration:    video.duration,
          url:         video.url,
          thumbnail:   video.thumbnail,
          description: cleanedDescription,
          transcript:  transcriptObj,
          comments,
        };

        // 7. Write to DB
        const inserted = await saveToPostgres([videoRecord], query, queryId);
        docsInserted += inserted;
        videosScraped++;

        // Checkpoint: record this video as successfully saved
        await saveCheckpoint(jobId, source, video.videoId);

        logger.info({
          jobId, source, status: 'progress',
          message: `Saved video "${video.title}"`,
          videoId: video.videoId,
          counts: { comments: comments.length, docsInserted },
        });

      } catch (err) {
        // Single video failure is non-fatal — log and continue
        logger.warn({ jobId, source, message: 'Failed to process video, skipping', videoId: video.videoId, error: err });
        skipped++;
      }
    }

    // Clear checkpoint on full success so a re-run starts fresh
    await clearCheckpoint(jobId, source);

    const elapsed = ((Date.now() - startTime) / 1000).toFixed(2);
    logger.info({
      jobId, source, status: 'done',
      message: 'YouTube scrape completed',
      counts: { videosScraped, docsInserted, skipped },
      elapsedSeconds: elapsed,
    });

    return { videosScraped, docsInserted, skipped };

  } catch (err) {
    logger.error({ jobId, source, status: 'error', message: 'YouTube scrape failed', error: err });
    throw err;
  }
}

module.exports = { scrape };
