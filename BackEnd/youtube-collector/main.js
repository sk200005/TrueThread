require('dotenv').config();
const readline = require('readline');

const { searchVideos } = require('./src/search');
const { getVideoMetadata } = require('./src/metadata');
const { cleanDescription, cleanTranscript } = require('./src/cleaner');
const { getTranscript } = require('./src/transcript');
const { translateTranscript } = require('./src/translator');
const { getTopComments } = require('./src/comments');
const { saveJson } = require('./src/output');

const TARGET_COUNT = 3;

// ─── Terminal Prompt ─────────────────────────────────────────────────────────

function askQuestion(prompt) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((resolve) => {
    rl.question(prompt, (answer) => {
      rl.close();
      resolve(answer.trim());
    });
  });
}

// ─── Main Orchestrator ───────────────────────────────────────────────────────

async function main() {
  // Validate API key before doing anything
  const apiKey = process.env.YOUTUBE_API_KEY;
  if (!apiKey || apiKey === 'your_youtube_api_key_here') {
    console.error(
      '\n❌  YOUTUBE_API_KEY is missing.\n' +
      '    1. Copy .env.example to .env\n' +
      '    2. Add your free API key (see .env.example for instructions)\n'
    );
    process.exit(1);
  }

  const query = await askQuestion('Enter your YouTube search query: ');
  if (!query) {
    console.error('❌  No query entered. Exiting.');
    process.exit(1);
  }

  const startTime = Date.now();
  const skipped = [];
  const collected = [];

  console.log('\n══════════════════════════════════════════════');
  console.log(' YouTube Knowledge Collector');
  console.log('══════════════════════════════════════════════');

  try {
    // 1. Search YouTube for candidate video IDs
    const videoIds = await searchVideos(query, apiKey);

    // 2. Fetch and rank full metadata in one batch call
    const rankedVideos = await getVideoMetadata(videoIds, apiKey);

    // 3. Process candidates in rank order until we have TARGET_COUNT
    console.log(`\n🎬 Processing candidates to find ${TARGET_COUNT} videos with transcripts...\n`);

    for (const video of rankedVideos) {
      if (collected.length >= TARGET_COUNT) break;

      console.log(`▶  [${collected.length + 1}/${TARGET_COUNT}] "${video.title}"`);
      console.log(`   Channel: ${video.channel} | Views: ${video.views.toLocaleString()} | Likes: ${video.likes.toLocaleString()}`);

      try {
        // 4. Fetch transcript (mandatory — skip if unavailable)
        const rawTranscript = await getTranscript(video.videoId);
        if (!rawTranscript) {
          console.log(`   ⏭  Skipped — no transcript available.\n`);
          skipped.push({ title: video.title, reason: 'No transcript available' });
          continue;
        }

        // 5. Clean transcript + description
        const cleanedTranscript = cleanTranscript(rawTranscript.text);
        const cleanedDescription = cleanDescription(video.description);

        // 6. Detect language & translate if not English
        const transcriptObj = await translateTranscript(cleanedTranscript);

        // 7. Get top comments
        const comments = await getTopComments(video.videoId, apiKey, 3);

        console.log(`   ✅ Transcript collected (${cleanedTranscript.length} chars, lang: ${transcriptObj.language})`);
        console.log(`   💬 Top comments collected: ${comments.length}\n`);

        collected.push({
          videoId: video.videoId,
          title: video.title,
          channel: video.channel,
          publishedAt: video.publishedAt,
          views: video.views,
          likes: video.likes,
          duration: video.duration,
          url: video.url,
          thumbnail: video.thumbnail,
          description: cleanedDescription,
          transcript: transcriptObj,
          comments: comments,
        });

      } catch (err) {
        console.warn(`   ⚠️  Error processing video: ${err.message}. Skipping.\n`);
        skipped.push({ title: video.title, reason: err.message });
      }
    }

    // 7. Save results
    const output = { query, videos: collected };
    saveJson(output);

    // 8. Print summary
    const elapsed = ((Date.now() - startTime) / 1000).toFixed(2);
    const totalChars = collected.reduce((sum, v) => sum + (v.transcript.english || v.transcript.original).length, 0);
    const avgLen = collected.length > 0 ? Math.round(totalChars / collected.length) : 0;

    console.log('\n══════════════════════════════════════════════');
    console.log(' Summary');
    console.log('══════════════════════════════════════════════');
    console.log(`  Query              : ${query}`);
    console.log(`  Videos collected   : ${collected.length}`);
    console.log(`  Videos skipped     : ${skipped.length}`);
    if (skipped.length > 0) {
      skipped.forEach((s) => console.log(`    • "${s.title}" → ${s.reason}`));
    }
    console.log(`  Transcripts saved  : ${collected.length}`);
    console.log(`  Avg transcript len : ${avgLen.toLocaleString()} chars`);
    console.log(`  Total time         : ${elapsed}s`);
    console.log('══════════════════════════════════════════════\n');

  } catch (err) {
    console.error(`\n❌  Fatal error: ${err.message}`);
    if (err.response?.data) {
      console.error('   API error details:', JSON.stringify(err.response.data, null, 2));
    }
    process.exit(1);
  }
}

main();
