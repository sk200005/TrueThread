"""
reddit_client.py — Fetches data from Reddit using Playwright.
"""

import logging
import urllib.parse
from typing import Any, List, Dict
from playwright.async_api import async_playwright, Page
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

async def search_posts(page: Page, query: str) -> List[str]:
    """Search Reddit and return top post URLs."""
    encoded_query = urllib.parse.quote(query)
    search_url = f"https://www.reddit.com/search/?q={encoded_query}&sort=relevance"
    
    logger.info("Navigating to Reddit search: %s", search_url)
    await page.goto(search_url, wait_until="domcontentloaded")
    
    try:
        # Wait for either shreddit-post or a comment link to appear
        await page.wait_for_selector('shreddit-post, a[href*="/comments/"]', state="attached", timeout=10000)
    except Exception:
        logger.warning("Timeout waiting for search results to appear.")
    
    post_urls = await page.evaluate('''() => {
        let posts = Array.from(document.querySelectorAll('shreddit-post'));
        let urls = [];
        if (posts.length > 0) {
            urls = posts.map(p => p.getAttribute('content-href') || (p.querySelector('a[href*="/comments/"]') && p.querySelector('a[href*="/comments/"]').getAttribute('href'))).filter(Boolean);
        } else {
            const links = Array.from(document.querySelectorAll('a[href*="/comments/"]'));
            urls = links.map(a => a.getAttribute('href')).filter(href => href && href.match(/\\/r\\/[^\\/]+\\/comments\\/[a-z0-9]+\\//));
        }
        urls = urls.map(url => url.startsWith('http') ? url : window.location.origin + url);
        return Array.from(new Set(urls)).slice(0, 5);
    }''')
    
    logger.info("Found %d post URLs.", len(post_urls))
    return post_urls


async def extract_post_data(page: Page, url: str) -> Dict[str, Any]:
    """Extract metadata and body from a post URL."""
    post_url = f"{url}&sort=top" if "?" in url else f"{url}?sort=top"
    logger.info("Navigating to post: %s", post_url)
    await page.goto(post_url, wait_until="domcontentloaded")
    
    try:
        await page.wait_for_selector('shreddit-post', state="attached", timeout=10000)
    except Exception:
        pass
        
    post_data = await page.evaluate('''() => {
        const el = document.querySelector('shreddit-post');
        if (!el) return null;
        const bodyEl = el.querySelector('div[slot="text-body"]');
        let body = '';
        if (bodyEl) {
            const pTags = Array.from(bodyEl.querySelectorAll('p'));
            body = pTags.length > 0 ? pTags.map(p => p.innerText.trim()).filter(Boolean).join('\\n') : bodyEl.innerText.trim();
        }
        return { 
            post_id: el.getAttribute('id') || '', 
            title: el.getAttribute('post-title') || '', 
            url: window.location.href, 
            subreddit: el.getAttribute('subreddit-prefixed-name') || '', 
            upvotes: el.getAttribute('score') || '', 
            body: body 
        };
    }''')
    
    if not post_data:
        return {"title": "Unknown", "url": url, "post_id": "unknown", "body": ""}
        
    if post_data.get("body"):
        # Handle the duplicate body bug from Reddit's new layout
        half = len(post_data["body"]) // 2
        if len(post_data["body"]) > 20 and post_data["body"][:half].strip() == post_data["body"][half:].strip():
            post_data["body"] = post_data["body"][:half].strip()
            
    return post_data


async def extract_comments(page: Page, post_id: str) -> List[Dict[str, Any]]:
    """Scroll down and extract top comments."""
    await page.evaluate("window.scrollBy(0, 500)")
    try:
        await page.wait_for_selector('shreddit-comment', state="attached", timeout=5000)
    except Exception:
        pass
        
    comments = await page.evaluate('''(postId) => {
        const results = [];
        const topLevelComments = Array.from(document.querySelectorAll('shreddit-comment[depth="0"]')).slice(0, 7);

        function getText(node) {
            const textNode = node.querySelector('div[slot="comment"]');
            if (!textNode) return '';
            const pTags = Array.from(textNode.querySelectorAll('p'));
            return pTags.length > 0 ? pTags.map(p => p.innerText.trim()).filter(Boolean).join('\\n') : textNode.innerText.trim();
        }

        for (const tlc of topLevelComments) {
            let text = getText(tlc);
            const half = Math.floor(text.length / 2);
            if (text.length > 20 && text.substring(0, half).trim() === text.substring(half).trim()) {
                text = text.substring(0, half).trim();
            }
            const timeEl = tlc.querySelector('time');
            results.push({
                id: tlc.getAttribute('thingid') || '',
                post_id: postId,
                author: tlc.getAttribute('author') || '',
                text: text,
                upvotes: tlc.getAttribute('score') || '',
                published_date: timeEl ? (timeEl.getAttribute('datetime') || timeEl.innerText) : '',
            });
        }
        return results;
    }''', post_id)
    
    return comments


async def scrape(query: str) -> List[Dict[str, Any]]:
    """Main orchestration function to scrape Reddit for a query."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()
        
        results = []
        try:
            @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=4))
            async def do_search():
                return await search_posts(page, query)
                
            post_urls = await do_search()
            
            for url in post_urls:
                try:
                    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=4))
                    async def fetch_post():
                        return await extract_post_data(page, url)
                        
                    post_data = await fetch_post()
                    if post_data["post_id"] == "unknown":
                        continue
                        
                    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=0.5, min=0.5, max=2))
                    async def fetch_comments():
                        return await extract_comments(page, post_data["post_id"])
                        
                    comments = await fetch_comments()
                    post_data["comments"] = comments
                    results.append(post_data)
                    
                except Exception as e:
                    logger.warning("Failed to process post %s: %s", url, e)
                    continue
                    
        finally:
            await browser.close()
            
        return results
