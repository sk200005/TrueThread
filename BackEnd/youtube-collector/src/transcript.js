const { YoutubeTranscript } = require('youtube-transcript');
const { execFile } = require('child_process');
const util = require('util');
const axios = require('axios');
const execFileAsync = util.promisify(execFile);

/**
 * Finds the best English subtitle key from a subtitles/captions object.
 * Matches 'en', 'en-US', 'en-GB', etc. — any key starting with 'en'.
 *
 * @param {object} subsObj - yt-dlp subtitles or automatic_captions object
 * @returns {string|null} The matching language key, or null
 */
function findEnglishKey(subsObj) {
  if (!subsObj || typeof subsObj !== 'object') return null;
  // Prefer exact 'en' first, then any 'en-*' variant
  if (subsObj['en']) return 'en';
  const variant = Object.keys(subsObj).find((k) => k.startsWith('en'));
  return variant || null;
}

/**
 * Fetches English transcript using yt-dlp.
 *
 * Subtitle priority:
 *   1. Manually created English subtitles (info.subtitles)
 *   2. Auto-generated English subtitles (info.automatic_captions)
 *   3. No English available → return null
 */
async function _fetchTranscriptYtdlp(videoId) {
  try {
    const url = `https://www.youtube.com/watch?v=${videoId}`;
    const args = [
      '--skip-download',
      '--write-subs',
      '--write-auto-subs',
      '--sub-format', 'json3',
      '--dump-json',
      url
    ];

    const { stdout } = await execFileAsync('yt-dlp', args, { maxBuffer: 1024 * 1024 * 10 });
    const info = JSON.parse(stdout);

    // ── Find English subtitles with priority: manual > auto-generated ────
    let lang = null;
    let subsSource = null;

    // 1. Prefer manually created English subtitles
    const manualEnKey = findEnglishKey(info.subtitles);
    if (manualEnKey) {
      lang = manualEnKey;
      subsSource = info.subtitles;
      console.log(`   📝 Using manually created English subtitles (${lang})`);
    }

    // 2. Fall back to auto-generated English subtitles
    if (!lang) {
      const autoEnKey = findEnglishKey(info.automatic_captions);
      if (autoEnKey) {
        lang = autoEnKey;
        subsSource = info.automatic_captions;
        console.log(`   🤖 Using auto-generated English subtitles (${lang})`);
      }
    }

    // 3. No English subtitles available at all
    if (!lang || !subsSource) {
      console.log(`   ⏭  No English subtitles available for ${videoId}`);
      return null;
    }

    const formats = subsSource[lang];
    let subFormat = formats.find(f => f.ext === 'json3') || formats[0];
    const subUrl = subFormat.url;

    if (!subUrl) return null;

    const resp = await axios.get(subUrl);

    let text = '';
    if (subFormat.ext === 'json3' || subUrl.includes('json3')) {
      const data = typeof resp.data === 'string' ? JSON.parse(resp.data) : resp.data;
      const events = data.events || [];
      const textParts = [];
      for (const event of events) {
        const segs = event.segs || [];
        for (const seg of segs) {
          textParts.push(seg.utf8 || '');
        }
      }
      text = textParts.join('').replace(/\n/g, ' ');
    } else {
      // Basic fallback cleanup for non-JSON formats like VTT
      text = resp.data.replace(/<[^>]+>/g, '')
                      .replace(/[\d:\.,]+ --> [\d:\.,]+/g, '')
                      .replace(/WEBVTT|Kind:|Language:|Style:|Align:|Position:/g, '');
    }

    // Clean up transcript text
    text = text.replace(/\[Music\]|\[Applause\]|\[.*?\]/gi, '').replace(/\s+/g, ' ').trim();

    if (!text) return null;

    return { text, lang: 'en' };
  } catch (err) {
    const errorMsg = err.message ? err.message.split('\n')[0] : String(err);
    console.warn(`   ⚠️  yt-dlp fetch failed for ${videoId}: ${errorMsg}`);
    return null;
  }
}

/**
 * Fetches English transcript using the youtube-transcript npm package.
 * Explicitly requests English language only.
 */
async function _fetchTranscriptApi(videoId) {
  try {
    const entries = await YoutubeTranscript.fetchTranscript(videoId, { lang: 'en' });
    if (!entries || entries.length === 0) return null;

    let text = entries.map((e) => e.text).join(' ');
    text = text.replace(/\[Music\]|\[Applause\]|\[.*?\]/gi, '').replace(/\s+/g, ' ').trim();
    
    return { text, lang: 'en' };
  } catch (err) {
    console.warn(`   ⚠️  youtube-transcript fetch failed for ${videoId}: ${err.message}`);
    return null;
  }
}

/**
 * Fetches the English transcript for a YouTube video.
 * Returns null if no English transcript is available (caller should skip the video).
 *
 * Priority:
 *   1. yt-dlp: manual English subs > auto-generated English subs
 *   2. youtube-transcript API: English only
 *   3. null (no English transcript available)
 *
 * @param {string} videoId - YouTube video ID
 * @returns {Promise<{ text: string, lang: string } | null>}
 */
async function getTranscript(videoId) {
  const transcript = await _fetchTranscriptYtdlp(videoId);
  if (transcript) {
    return transcript;
  }

  return await _fetchTranscriptApi(videoId);
}

module.exports = { getTranscript };

