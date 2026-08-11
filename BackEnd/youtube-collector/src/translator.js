const { franc } = require('franc');

// @vitalets/google-translate-api v9+ is ESM-only, so we use a dynamic import()
// inside an async function to load it from CommonJS.
let _translate;
async function getTranslateFn() {
  if (!_translate) {
    const mod = await import('@vitalets/google-translate-api');
    _translate = mod.translate;
  }
  return _translate;
}

const CHUNK_SIZE = 4500; // Google Translate public endpoint limit is ~5000 chars
const ENGLISH_LANGS = new Set(['eng', 'und']); // franc language codes for English / undetermined

/**
 * Detects the language of the transcript and translates it to English if needed.
 *
 * @param {string} text - The cleaned transcript text
 * @returns {Promise<{ language: string, original: string, english: string }>}
 */
async function translateTranscript(text) {
  const detectedLang = franc(text, { minLength: 20 });

  if (ENGLISH_LANGS.has(detectedLang)) {
    // Already English — no translation needed
    return { language: 'en', original: text, english: text };
  }

  console.log(`   🌐 Non-English transcript detected (${detectedLang}). Translating...`);

  try {
    const translated = await translateInChunks(text);
    return { language: detectedLang, original: text, english: translated };
  } catch (err) {
    console.warn(`   ⚠️  Translation failed: ${err.message}. Storing original only.`);
    return { language: detectedLang, original: text, english: '' };
  }
}

/**
 * Splits text into chunks and translates each chunk individually
 * to respect the public endpoint's character limit.
 *
 * @param {string} text
 * @returns {Promise<string>}
 */
async function translateInChunks(text) {
  const translate = await getTranslateFn();
  const chunks = [];
  for (let i = 0; i < text.length; i += CHUNK_SIZE) {
    chunks.push(text.slice(i, i + CHUNK_SIZE));
  }

  const results = [];
  for (const chunk of chunks) {
    const translatePromise = translate(chunk, { to: 'en' });
    const timeoutPromise = new Promise((_, reject) => 
      setTimeout(() => reject(new Error('Translation API timed out after 10 seconds')), 10000)
    );
    const res = await Promise.race([translatePromise, timeoutPromise]);
    results.push(res.text);
  }

  return results.join(' ');
}

module.exports = { translateTranscript };
