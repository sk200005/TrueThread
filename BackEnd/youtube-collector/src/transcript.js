const { YoutubeTranscript } = require('youtube-transcript');
const { execFile } = require('child_process');
const util = require('util');
const axios = require('axios');
const execFileAsync = util.promisify(execFile);

/**
 * Fetches transcript using yt-dlp.
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

    const allSubs = {};
    if (info.automatic_captions) Object.assign(allSubs, info.automatic_captions);
    if (info.subtitles) Object.assign(allSubs, info.subtitles);

    if (Object.keys(allSubs).length === 0) {
      return null;
    }

    let lang = 'en';
    if (!allSubs[lang]) {
      lang = Object.keys(allSubs)[0];
    }

    const formats = allSubs[lang];
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

    return { text, lang };
  } catch (err) {
    const errorMsg = err.message ? err.message.split('\n')[0] : String(err);
    console.warn(`   ⚠️  yt-dlp fetch failed for ${videoId}: ${errorMsg}`);
    return null;
  }
}

/**
 * Fetches and concatenates the full transcript for a YouTube video using youtube-transcript.
 */
async function _fetchTranscriptApi(videoId) {
  try {
    const entries = await YoutubeTranscript.fetchTranscript(videoId);
    if (!entries || entries.length === 0) return null;

    let text = entries.map((e) => e.text).join(' ');
    text = text.replace(/\[Music\]|\[Applause\]|\[.*?\]/gi, '').replace(/\s+/g, ' ').trim();
    
    const lang = entries[0]?.lang || 'en';
    return { text, lang };
  } catch (err) {
    console.warn(`   ⚠️  youtube-transcript fetch failed for ${videoId}: ${err.message}`);
    return null;
  }
}

/**
 * Fetches and concatenates the full transcript for a YouTube video.
 * Returns null if no transcript is available (caller should skip the video).
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
