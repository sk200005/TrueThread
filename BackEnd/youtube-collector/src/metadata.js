const axios = require('axios');

const YOUTUBE_VIDEOS_URL = 'https://www.googleapis.com/youtube/v3/videos';
const TARGET_VIDEO_COUNT = 5;
const MAX_DURATION_SECONDS = 1200; // 20 minutes — videos longer than this are skipped

/**
 * Fetches full metadata for a batch of video IDs in a single API call,
 * filters out videos exceeding MAX_DURATION_SECONDS, applies verified-channel
 * preference when available, and returns the top candidates sorted by relevance.
 *
 * @param {string[]} videoIds - Array of YouTube video IDs
 * @param {string} apiKey - YouTube Data API v3 key
 * @returns {Promise<object[]>} Ranked array of video metadata objects
 */
async function getVideoMetadata(videoIds, apiKey) {
  console.log(`\n📊 Fetching metadata for ${videoIds.length} candidates in one batch call...`);

  const response = await axios.get(YOUTUBE_VIDEOS_URL, {
    params: {
      key: apiKey,
      id: videoIds.join(','),
      part: 'snippet,statistics,contentDetails',
    },
  });

  const items = response.data.items || [];

  // Build metadata objects
  const videos = items.map((item) => {
    const snippet = item.snippet || {};
    const stats = item.statistics || {};
    const details = item.contentDetails || {};

    const views = parseInt(stats.viewCount || 0, 10);
    const likes = parseInt(stats.likeCount || 0, 10);
    const isoDuration = details.duration || 'PT0S';

    return {
      videoId: item.id,
      title: snippet.title || '',
      channel: snippet.channelTitle || '',
      channelId: snippet.channelId || '',
      // NOTE: YouTube Data API v3 does NOT expose channel verification status.
      // The verification badge is rendered client-side by YouTube's frontend and
      // is not available via any public API endpoint. This field exists as a hook
      // for future use (e.g., if YouTube adds the field or a supplementary data
      // source is integrated). Do NOT fabricate verification from subscriber
      // count, popularity, or channel-name heuristics.
      isChannelVerified: null,
      publishedAt: snippet.publishedAt || '',
      views: views,
      likes: likes,
      duration: parseDuration(isoDuration),
      durationSeconds: parseDurationSeconds(isoDuration),
      url: `https://www.youtube.com/watch?v=${item.id}`,
      thumbnail:
        snippet.thumbnails?.maxres?.url ||
        snippet.thumbnails?.high?.url ||
        snippet.thumbnails?.medium?.url ||
        '',
      description: snippet.description || '',
    };
  });

  // ── Duration filter: skip videos longer than 15 minutes ────────────────
  const beforeCount = videos.length;
  const filtered = videos.filter((v) => v.durationSeconds <= MAX_DURATION_SECONDS);
  const droppedCount = beforeCount - filtered.length;
  if (droppedCount > 0) {
    console.log(`   ⏱  Filtered out ${droppedCount} video(s) exceeding ${MAX_DURATION_SECONDS}s (15 min).`);
  }

  // ── Sort: original search relevance, with verified-channel tiebreaker ──
  // Verified channels (isChannelVerified === true) are promoted ahead of
  // unverified/unknown channels at the same relevance position.
  // Currently isChannelVerified is always null, so this is a no-op — but the
  // logic is ready for when verification data becomes available.
  filtered.sort((a, b) => {
    const aVerified = a.isChannelVerified === true ? 0 : 1;
    const bVerified = b.isChannelVerified === true ? 0 : 1;
    if (aVerified !== bVerified) return aVerified - bVerified;
    return videoIds.indexOf(a.videoId) - videoIds.indexOf(b.videoId);
  });

  const top = filtered.slice(0, TARGET_VIDEO_COUNT * 2); // Extra buffer — transcripts will filter further

  console.log(`   Top ${Math.min(top.length, TARGET_VIDEO_COUNT)} candidates selected after ranking.`);
  return top;
}

/**
 * Converts ISO 8601 duration (e.g. PT4M13S) to total seconds.
 *
 * @param {string} iso - ISO 8601 duration string (e.g. "PT1H20M5S")
 * @returns {number} Total duration in seconds
 */
function parseDurationSeconds(iso) {
  const match = iso.match(/PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?/);
  if (!match) return 0;
  const h = parseInt(match[1] || 0, 10);
  const m = parseInt(match[2] || 0, 10);
  const s = parseInt(match[3] || 0, 10);
  return (h * 3600) + (m * 60) + s;
}

/**
 * Converts ISO 8601 duration (e.g. PT4M13S) to a human-readable string (e.g. "4:13").
 */
function parseDuration(iso) {
  const match = iso.match(/PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?/);
  if (!match) return '0:00';
  const h = parseInt(match[1] || 0, 10);
  const m = parseInt(match[2] || 0, 10);
  const s = parseInt(match[3] || 0, 10);
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

module.exports = { getVideoMetadata, parseDurationSeconds };
