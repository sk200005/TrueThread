'use strict';

/**
 * scraper.js — Hardened Reddit scraper (importable module).
 *
 * Extracted from reddit-test.js and upgraded with:
 *   - Retry with exponential backoff on every network call
 *   - Checkpoint: saves last processed post_id so retries resume mid-scrape
 *   - Structured JSON logging for every step
 *
 * Exports:
 *   scrape(jobId, query, queryId) → Promise<{ postsScraped, docsInserted, skipped }>
 *
 * The original reddit-test.js CLI entry point still works independently.
 * This module only handles the core scrape + DB-write logic.
 */

require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const { chromium } = require('playwright');
const path = require('path');
const logger = require('../shared/logger');
const { withRetry } = require('../shared/retry');
const { saveCheckpoint, loadCheckpoint, clearCheckpoint } = require('../shared/checkpoint');
const { saveToPostgres } = require('./db-helper');

const HEADLESS_MODE = true;

// ── DOM extraction helpers (identical logic to reddit-test.js) ────────────────

async function searchPosts(page, query) {
  const encodedQuery = encodeURIComponent(query);
  const searchUrl = `https://www.reddit.com/search/?q=${encodedQuery}&sort=relevance`;
  await page.goto(searchUrl, { waitUntil: 'domcontentloaded' });
  try {
    await page.locator('shreddit-post, a[href*="/comments/"]').first().waitFor({ state: 'attached', timeout: 10000 });
  } catch (_) {}

  const postUrls = await page.evaluate(() => {
    let posts = Array.from(document.querySelectorAll('shreddit-post'));
    let urls = [];
    if (posts.length > 0) {
      urls = posts.map(p => p.getAttribute('content-href') || p.querySelector('a[href*="/comments/"]')?.getAttribute('href')).filter(Boolean);
    } else {
      const links = Array.from(document.querySelectorAll('a[href*="/comments/"]'));
      urls = links.map(a => a.getAttribute('href')).filter(href => href && href.match(/\/r\/[^\/]+\/comments\/[a-z0-9]+\//));
    }
    urls = urls.map(url => url.startsWith('http') ? url : window.location.origin + url);
    return [...new Set(urls)].slice(0, 5);
  });
  return postUrls;
}

async function extractPostData(page, url) {
  const postUrl = url.includes('?') ? `${url}&sort=top` : `${url}?sort=top`;
  await page.goto(postUrl, { waitUntil: 'domcontentloaded' });
  try {
    await page.locator('shreddit-post').first().waitFor({ state: 'attached', timeout: 10000 });
  } catch (_) {}

  const postData = await page.evaluate(() => {
    const el = document.querySelector('shreddit-post');
    if (!el) return null;
    const bodyEl = el.querySelector('div[slot="text-body"]');
    let body = '';
    if (bodyEl) {
      const pTags = Array.from(bodyEl.querySelectorAll('p'));
      body = pTags.length > 0 ? pTags.map(p => p.innerText.trim()).filter(Boolean).join('\n') : bodyEl.innerText.trim();
    }
    return { post_id: el.getAttribute('id') || '', title: el.getAttribute('post-title') || '', url: window.location.href, subreddit: el.getAttribute('subreddit-prefixed-name') || '', upvotes: el.getAttribute('score') || '', body };
  });

  if (postData?.body) {
    const half = Math.floor(postData.body.length / 2);
    if (postData.body.length > 20 && postData.body.substring(0, half).trim() === postData.body.substring(half).trim()) {
      postData.body = postData.body.substring(0, half).trim();
    }
  }
  return postData || { title: 'Unknown', url, post_id: 'unknown' };
}

async function extractComments(page, postId) {
  await page.evaluate(() => window.scrollBy(0, 500));
  try {
    await page.locator('shreddit-comment').first().waitFor({ state: 'attached', timeout: 5000 });
  } catch (_) {}

  return page.evaluate((postId) => {
    const results = [];
    const topLevelComments = Array.from(document.querySelectorAll('shreddit-comment[depth="0"]')).slice(0, 7);

    function getText(node) {
      const textNode = node.querySelector('div[slot="comment"]');
      if (!textNode) return '';
      const pTags = Array.from(textNode.querySelectorAll('p'));
      return pTags.length > 0 ? pTags.map(p => p.innerText.trim()).filter(Boolean).join('\n') : textNode.innerText.trim();
    }

    function processComment(node, isTopLevel) {
      let text = getText(node);
      const half = Math.floor(text.length / 2);
      if (text.length > 20 && text.substring(0, half).trim() === text.substring(half).trim()) text = text.substring(0, half).trim();
      const timeEl = node.querySelector('time');
      results.push({
        id: node.getAttribute('thingid') || '',
        post_id: postId,
        parent_comment_id: isTopLevel ? null : (node.getAttribute('parentid') || null),
        author: node.getAttribute('author') || '',
        text,
        upvotes: node.getAttribute('score') || '',
        published_date: timeEl ? (timeEl.getAttribute('datetime') || timeEl.innerText) : '',
      });
    }

    for (const tlc of topLevelComments) {
      processComment(tlc, true);
    }
    return results;
  }, postId);
}

// ── Main export ───────────────────────────────────────────────────────────────

/**
 * scrape — Run the full Reddit scrape for a query.
 *
 * @param {string}      jobId   — UUID from queries.id (null for standalone CLI runs)
 * @param {string}      query   — The search query
 * @param {string|null} queryId — Same as jobId (alias for db-helper compatibility)
 * @returns {Promise<{ postsScraped: number, docsInserted: number, skippedPosts: number }>}
 */
async function scrape(jobId, query, queryId = null) {
  const source = 'reddit';
  const startTime = Date.now();

  logger.info({ jobId, source, status: 'started', message: 'Reddit scrape started', query });

  // Load checkpoint — returns null if this is a fresh job
  const lastProcessedId = await loadCheckpoint(jobId, source);
  if (lastProcessedId) {
    logger.info({ jobId, source, message: `Resuming from checkpoint. Last processed post_id: ${lastProcessedId}` });
  }

  const browser = await chromium.launch({ headless: HEADLESS_MODE });
  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    viewport: { width: 1280, height: 720 },
  });
  const page = await context.newPage();

  let postsScraped = 0;
  let docsInserted = 0;
  let skippedPosts = 0;
  let foundCheckpoint = !lastProcessedId; // If no checkpoint, start from the beginning

  try {
    // Retry the search itself (network can be flaky)
    const postUrls = await withRetry(() => searchPosts(page, query), {
      maxRetries: 3, baseDelayMs: 1000, label: 'reddit-search',
    });

    logger.info({ jobId, source, message: `Found ${postUrls.length} post URLs` });

    for (const url of postUrls) {
      try {
        // Retry individual post fetches with backoff
        const postData = await withRetry(() => extractPostData(page, url), {
          maxRetries: 3, baseDelayMs: 800, label: `reddit-post:${url}`,
        });

        // Checkpoint resume: skip posts we already processed in a previous run
        if (!foundCheckpoint) {
          if (postData.post_id === lastProcessedId) {
            foundCheckpoint = true; // Found the checkpoint — next post is new
          }
          logger.debug({ jobId, source, message: `Skipping already-processed post`, post_id: postData.post_id });
          skippedPosts++;
          continue;
        }

        const comments = await withRetry(() => extractComments(page, postData.post_id), {
          maxRetries: 2, baseDelayMs: 500, label: `reddit-comments:${postData.post_id}`,
        });

        postData.comments = comments;

        // Write this post + its comments to DB
        const inserted = await saveToPostgres([postData], query, queryId);
        docsInserted += inserted;
        postsScraped++;

        // Checkpoint: record this post as successfully saved
        await saveCheckpoint(jobId, source, postData.post_id);

        logger.info({
          jobId, source, status: 'progress',
          message: `Saved post "${postData.title}"`,
          post_id: postData.post_id,
          counts: { comments: comments.length, docsInserted },
        });

      } catch (err) {
        // Single post failure is non-fatal — log and continue with next post
        logger.warn({ jobId, source, message: `Failed to process post, skipping`, url, error: err });
        skippedPosts++;
      }
    }

    // Clear checkpoint on full success so a re-run starts fresh
    await clearCheckpoint(jobId, source);

    const elapsed = ((Date.now() - startTime) / 1000).toFixed(2);
    logger.info({
      jobId, source, status: 'done',
      message: 'Reddit scrape completed',
      counts: { postsScraped, docsInserted, skippedPosts },
      elapsedSeconds: elapsed,
    });

    return { postsScraped, docsInserted, skippedPosts };

  } catch (err) {
    logger.error({ jobId, source, status: 'error', message: 'Reddit scrape failed', error: err });
    throw err;
  } finally {
    await browser.close();
  }
}

module.exports = { scrape };
