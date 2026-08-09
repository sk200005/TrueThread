const { chromium } = require('playwright');
const fs = require('fs');

const HEADLESS_MODE = true;

async function searchPosts(page, query) {
  console.log(`[1] Searching for: "${query}" (sorted by relevance)`);
  const encodedQuery = encodeURIComponent(query);
  const searchUrl = `https://www.reddit.com/search/?q=${encodedQuery}&sort=relevance`;
  
  await page.goto(searchUrl, { waitUntil: 'domcontentloaded' });
  
  console.log('Waiting for search results to load...');
  try {
    await page.locator('shreddit-post, a[href*="/comments/"]').first().waitFor({ state: 'attached', timeout: 10000 });
  } catch (e) {
    console.log("Timeout waiting for search results.");
  }
  
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
  
  console.log(`Found ${postUrls.length} post URLs.`);
  return postUrls;
}

async function extractPostData(page, url) {
  const postUrl = url.includes('?') ? `${url}&sort=top` : `${url}?sort=top`;
  console.log(`[2] Extracting post data from: ${postUrl}`);
  await page.goto(postUrl, { waitUntil: 'domcontentloaded' });
  
  try {
    await page.locator('shreddit-post').first().waitFor({ state: 'attached', timeout: 10000 });
  } catch (e) {}
  
  const postData = await page.evaluate(() => {
    const postLocator = document.querySelector('shreddit-post');
    if (postLocator) {
      const title = postLocator.getAttribute('post-title') || '';
      const subreddit = postLocator.getAttribute('subreddit-prefixed-name') || '';
      const upvotes = postLocator.getAttribute('score') || '';
      const id = postLocator.getAttribute('id') || '';
      
      const bodyLocator = postLocator.querySelector('div[slot="text-body"]');
      let body = '';
      if (bodyLocator) {
        // Fix for text duplication: grab text only from explicit paragraph/text nodes to avoid slot duplication
        const pTags = Array.from(bodyLocator.querySelectorAll('p'));
        if (pTags.length > 0) {
          body = pTags.map(p => p.innerText.trim()).filter(Boolean).join('\n');
        } else {
          body = bodyLocator.innerText.trim();
        }
      }
      
      return { post_id: id, title, url: window.location.href, subreddit, upvotes, body };
    }
    return null;
  });

  if (postData && postData.body) {
     // Deduplicate text if the innerText grabs duplicate shadow/light dom text
     const half = Math.floor(postData.body.length / 2);
     if (postData.body.length > 20 && postData.body.substring(0, half).trim() === postData.body.substring(half).trim()) {
        postData.body = postData.body.substring(0, half).trim();
     }
  }

  return postData || { title: 'Unknown', url: postUrl, post_id: 'unknown' };
}

async function extractComments(page, postId) {
  console.log(`[3] Extracting comments for post ${postId}...`);
  
  await page.evaluate(() => window.scrollBy(0, 500));
  try {
    await page.locator('shreddit-comment').first().waitFor({ state: 'attached', timeout: 5000 });
  } catch (e) {}
  
  const commentsData = await page.evaluate((postId) => {
    const results = [];
    
    // Find all top-level comments (depth 0), limit to top 7
    const topLevelComments = Array.from(document.querySelectorAll('shreddit-comment[depth="0"]')).slice(0, 7);
    
    function extractText(commentNode) {
      const textNode = commentNode.querySelector('div[slot="comment"]');
      if (!textNode) return '';
      
      const pTags = Array.from(textNode.querySelectorAll('p'));
      if (pTags.length > 0) {
        return pTags.map(p => p.innerText.trim()).filter(Boolean).join('\n');
      }
      return textNode.innerText.trim();
    }
    
    function processComment(commentNode, isTopLevel) {
      const id = commentNode.getAttribute('thingid') || '';
      let parentId = commentNode.getAttribute('parentid') || '';
      if (isTopLevel) {
        parentId = null;
      }
      
      const author = commentNode.getAttribute('author') || '';
      const upvotes = commentNode.getAttribute('score') || '';
      const timeEl = commentNode.querySelector('time');
      const publishedDate = timeEl ? (timeEl.getAttribute('datetime') || timeEl.innerText) : '';
      
      let text = extractText(commentNode);
      
      const half = Math.floor(text.length / 2);
      if (text.length > 20 && text.substring(0, half).trim() === text.substring(half).trim()) {
        text = text.substring(0, half).trim();
      }
      
      results.push({
        id,
        post_id: postId,
        parent_comment_id: parentId,
        author,
        text,
        upvotes,
        published_date: publishedDate
      });
      
      return id;
    }
    
    for (const tlc of topLevelComments) {
      processComment(tlc, true);
    }
    
    return results;
  }, postId);
  
  return commentsData;
}

async function main() {
  const args = process.argv.slice(2);
  let query = args[0];
  
  if (!query) {
    const readline = require('readline').createInterface({
      input: process.stdin,
      output: process.stdout
    });
    
    query = await new Promise(resolve => {
      readline.question('Enter search query: ', (answer) => {
        readline.close();
        resolve(answer.trim());
      });
    });
    
    if (!query) {
      console.error("No query provided. Exiting.");
      process.exit(1);
    }
  }
  console.log(`Starting run for query: ${query}`);
  
  const browser = await chromium.launch({ headless: HEADLESS_MODE });
  const context = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    viewport: { width: 1280, height: 720 }
  });
  
  const page = await context.newPage();
  const finalResults = [];
  
  try {
    const postUrls = await searchPosts(page, query);
    
    for (const url of postUrls) {
      try {
        const postData = await extractPostData(page, url);
        const comments = await extractComments(page, postData.post_id);
        
        postData.comments = comments;
        finalResults.push(postData);
      } catch (err) {
        console.error(`Error processing post ${url}:`, err.message);
      }
    }
    
    // Save the output to reddit-results.json
    const outputPath = 'reddit-results.json';
    fs.writeFileSync(outputPath, JSON.stringify(finalResults, null, 2), 'utf8');
    console.log(`\nSuccessfully scraped ${finalResults.length} posts and saved results to ${outputPath}`);

    // Save the output to PostgreSQL
    try {
      const { saveToPostgres } = require('./db-helper.js');
      console.log('Saving to PostgreSQL database...');
      await saveToPostgres(finalResults, query);
    } catch (dbErr) {
      console.error('Could not save to Postgres:', dbErr.message);
    }

    // ── Auto-run claim extraction ──────────────────────────────────────────
    // Spawn claim-extractor.js as a child process, streaming its output
    // directly to this terminal (inherit stdio) so you can watch it live.
    console.log('\n=============================================================');
    console.log(' STARTING CLAIM EXTRACTION PIPELINE...');
    console.log('=============================================================\n');

    const { spawnSync } = require('child_process');
    const extractorPath = require('path').join(__dirname, 'claim-extractor.js');
    const result = spawnSync('node', [extractorPath, outputPath], {
      stdio: 'inherit',   // streams stdout + stderr directly to this terminal
      cwd: require('path').dirname(__dirname),  // run from project root (where .env lives)
    });

    if (result.error) {
      console.error('\nFailed to launch claim-extractor.js:', result.error.message);
    } else if (result.status !== 0) {
      console.error(`\nClaim extractor exited with code ${result.status}`);
    }

  } catch (error) {
    console.error("An error occurred during scraping:", error);
  } finally {
    await browser.close();
  }
}

main();
