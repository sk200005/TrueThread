"""
youtube_client.py — Fetches data from YouTube using Data API v3 and youtube-transcript-api.
"""

import logging
import re
from typing import Any, List, Dict, Optional
import httpx
from youtube_transcript_api import YouTubeTranscriptApi
from app.core.config import settings

logger = logging.getLogger(__name__)

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_COMMENTS_URL = "https://www.googleapis.com/youtube/v3/commentThreads"

async def search_videos(query: str, max_results: int = 20) -> List[str]:
    """Search YouTube for a query and return a list of video IDs."""
    logger.info("Searching YouTube for: %r", query)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            YOUTUBE_SEARCH_URL,
            params={
                "key": settings.youtube_api_key,
                "q": query,
                "part": "snippet",
                "type": "video",
                "maxResults": max_results,
                "order": "relevance",
            }
        )
        resp.raise_for_status()
        data = resp.json()
        
    items = data.get("items", [])
    video_ids = [item["id"]["videoId"] for item in items if item.get("id", {}).get("videoId")]
    logger.info("Found %d candidate videos.", len(video_ids))
    return video_ids


def parse_duration(iso_duration: str) -> str:
    """Converts ISO 8601 duration (e.g. PT4M13S) to '4:13'."""
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso_duration)
    if not match:
        return '0:00'
    h = int(match.group(1) or 0)
    m = int(match.group(2) or 0)
    s = int(match.group(3) or 0)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


async def get_video_metadata(video_ids: List[str], target_count: int = 5) -> List[Dict[str, Any]]:
    """Fetch metadata for a batch of video IDs and return ranked list."""
    if not video_ids:
        return []
        
    logger.info("Fetching metadata for %d candidates in one batch call...", len(video_ids))
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            YOUTUBE_VIDEOS_URL,
            params={
                "key": settings.youtube_api_key,
                "id": ",".join(video_ids),
                "part": "snippet,statistics,contentDetails",
            }
        )
        resp.raise_for_status()
        data = resp.json()

    items = data.get("items", [])
    videos = []
    
    for item in items:
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        details = item.get("contentDetails", {})
        
        videos.append({
            "videoId": item["id"],
            "title": snippet.get("title", ""),
            "channel": snippet.get("channelTitle", ""),
            "publishedAt": snippet.get("publishedAt", ""),
            "views": int(stats.get("viewCount", 0)),
            "likes": int(stats.get("likeCount", 0)),
            "duration": parse_duration(details.get("duration", "PT0S")),
            "url": f"https://www.youtube.com/watch?v={item['id']}",
            "description": snippet.get("description", ""),
        })

    # Sort by original search relevance order
    videos.sort(key=lambda x: video_ids.index(x["videoId"]) if x["videoId"] in video_ids else 999)
    top_videos = videos[:target_count * 2] # buffer
    logger.info("Selected top %d candidates after ranking.", min(len(top_videos), target_count))
    return top_videos


async def get_top_comments(video_id: str, max_results: int = 3) -> List[Dict[str, Any]]:
    """Fetch the top comments for a given video ID."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            YOUTUBE_COMMENTS_URL,
            params={
                "key": settings.youtube_api_key,
                "videoId": video_id,
                "part": "snippet",
                "order": "relevance",
                "maxResults": max_results,
            }
        )
        if resp.status_code in (403, 404):
            logger.warning("Comments are disabled or not found for video %s.", video_id)
            return []
        resp.raise_for_status()
        data = resp.json()

    comments = []
    items = data.get("items", [])
    for item in items:
        comment_snippet = item["snippet"]["topLevelComment"]["snippet"]
        comments.append({
            "author": comment_snippet.get("authorDisplayName", ""),
            "text": comment_snippet.get("textDisplay", ""),
            "likes": comment_snippet.get("likeCount", 0),
            "publishedAt": comment_snippet.get("publishedAt", ""),
        })
    return comments


def get_transcript(video_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch transcript for a video, translating to English if necessary.
    Uses youtube_transcript_api (which handles translation natively).
    """
    try:
        import os
        import http.cookiejar
        from requests import Session
        
        session = Session()
        try:
            # Resolves to /Users/swayam/Desktop/Test/BackEnd/backend-python/cookies.txt
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__))) 
            cookies_path = os.path.join(base_dir, "cookies.txt")
            if os.path.exists(cookies_path):
                cookie_jar = http.cookiejar.MozillaCookieJar(cookies_path)
                cookie_jar.load(ignore_discard=True, ignore_expires=True)
                session.cookies.update(cookie_jar)
        except Exception as e:
            logger.warning("Failed to load cookies.txt: %s", e)
            
        ytt_api = YouTubeTranscriptApi(http_client=session)
        transcript_list = ytt_api.list(video_id)
        
        is_translated = False
        try:
            # Try to find an English transcript first
            transcript = transcript_list.find_transcript(['en'])
        except Exception:
            # If no english transcript, get the first available one and translate it
            transcript = transcript_list.find_transcript([t.language_code for t in transcript_list])
            if transcript.language_code != 'en':
                try:
                    transcript = transcript.translate('en')
                    is_translated = True
                except Exception as e:
                    logger.warning("Translation failed for %s: %s. Storing original.", video_id, e)
                
        entries = transcript.fetch()
        if not entries:
            return None
            
        text_parts = []
        for e in entries:
            part = e.get("text") if isinstance(e, dict) else getattr(e, "text", "")
            text_parts.append(part)
        text = " ".join(text_parts)
        
        # Clean up some common transcript artifacts
        text = re.sub(r'\[Music\]|\[Applause\]|\[.*?\]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        
        return {
            "text": text,
            "lang": 'en' if is_translated else transcript.language_code,
            "original_lang": transcript.language_code
        }
    except Exception as e:
        logger.warning("No transcript available for %s: %s", video_id, e)
        return None
