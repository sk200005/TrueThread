"""
youtube_client.py — Fetches data from YouTube using Data API v3 and youtube-transcript-api.
"""

import logging
import re
import os
import http.cookiejar
from requests import Session
from typing import Any, List, Dict, Optional, Tuple
import httpx
from youtube_transcript_api import (
    AgeRestricted,
    CookieError,
    CookieInvalid,
    InvalidVideoId,
    IpBlocked,
    NoTranscriptFound,
    NotTranslatable,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    TranslationLanguageNotAvailable,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeRequestFailed,
    YouTubeTranscriptApi,
)
from app.core.config import settings

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
COOKIES_PATH = os.path.join(_BASE_DIR, "cookies.txt")
if not os.path.exists(COOKIES_PATH):
    COOKIES_PATH = "cookies.txt"


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
            "thumbnail_url": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
        })

    # Sort by original search relevance order
    videos.sort(key=lambda x: video_ids.index(x["videoId"]) if x["videoId"] in video_ids else 999)
    logger.info(
        "Prepared %d ranked candidates for transcript collection (target=%d).",
        len(videos),
        target_count,
    )
    return videos


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


def _format_transcript_error(exc: Exception) -> str:
    """Return a short, user-useful reason for a transcript fetch failure."""
    if isinstance(exc, TranscriptsDisabled):
        return "transcripts disabled by video owner"
    if isinstance(exc, NoTranscriptFound):
        return "no caption track available"
    if isinstance(exc, VideoUnavailable):
        return "video unavailable"
    if isinstance(exc, VideoUnplayable):
        return "video unplayable"
    if isinstance(exc, AgeRestricted):
        return "age restricted"
    if isinstance(exc, InvalidVideoId):
        return "invalid video id"
    if isinstance(exc, (RequestBlocked, IpBlocked, PoTokenRequired)):
        return "YouTube blocked transcript request"
    if isinstance(exc, (CookieError, CookieInvalid)):
        return "invalid YouTube cookies"
    if isinstance(exc, (NotTranslatable, TranslationLanguageNotAvailable)):
        return "transcript is not translatable to English"
    if isinstance(exc, YouTubeRequestFailed):
        return "YouTube transcript request failed"

    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return msg[:300]


def _clean_transcript_text(text: str) -> str:
    """Clean up common transcript artifacts like [Music] and extra whitespace."""
    text = re.sub(r'\[Music\]|\[Applause\]|\[.*?\]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _fetch_transcript_ytdlp(video_id: str) -> Optional[Dict[str, Any]]:
    """Primary strategy: use yt-dlp to extract subtitles."""
    import yt_dlp
    import requests
    import json
    
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        'skip_download': True,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitlesformat': 'json3',
        'quiet': True,
        'no_warnings': True,
    }
    if os.path.exists(COOKIES_PATH):
        ydl_opts['cookiefile'] = COOKIES_PATH

    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            all_subs = {}
            if info.get('automatic_captions'):
                all_subs.update(info['automatic_captions'])
            if info.get('subtitles'):
                all_subs.update(info['subtitles'])
                
            if not all_subs:
                return None
                
            # Prefer English
            lang = 'en'
            if lang not in all_subs:
                lang = list(all_subs.keys())[0]
                
            formats = all_subs[lang]
            
            # Prefer json3 for easy parsing
            sub_format = next((f for f in formats if f.get('ext') == 'json3'), formats[0])
            sub_url = sub_format.get('url')
            
            if not sub_url:
                return None
                
            resp = requests.get(sub_url)
            resp.raise_for_status()
            
            text = ""
            if sub_format.get('ext') == 'json3' or 'json3' in sub_url:
                try:
                    data = resp.json()
                    events = data.get('events', [])
                    text_parts = []
                    for event in events:
                        segs = event.get('segs', [])
                        for seg in segs:
                            text_parts.append(seg.get('utf8', ''))
                    text = "".join(text_parts).replace('\n', ' ')
                except json.JSONDecodeError:
                    text = resp.text
            else:
                # Basic fallback cleanup for non-JSON formats like VTT
                text = re.sub(r'<[^>]+>', '', resp.text)
                text = re.sub(r'[\d:\.,]+ --> [\d:\.,]+', '', text)
                text = re.sub(r'WEBVTT|Kind:|Language:|Style:|Align:|Position:', '', text)
                
            text = _clean_transcript_text(text)
            if not text:
                return None
                
            return {
                "text": text,
                "lang": lang,
                "original_lang": lang
            }
    except Exception as e:
        logger.warning("yt-dlp transcript fetch failed for %s: %s", video_id, e)
        return None


def _fetch_transcript_api(video_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fallback strategy: use youtube-transcript-api."""
    try:
        if os.path.exists(COOKIES_PATH):
            session = Session()
            cookie_jar = http.cookiejar.MozillaCookieJar(COOKIES_PATH)
            cookie_jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(cookie_jar)
            ytt_api = YouTubeTranscriptApi(http_client=session)
        else:
            ytt_api = YouTubeTranscriptApi()
            
        transcript_list = ytt_api.list(video_id)
        
        is_translated = False
        try:
            # Try to find an English transcript first
            transcript = transcript_list.find_transcript(['en'])
        except Exception:
            # If no english transcript, get the first available one and translate it
            transcript = transcript_list.find_transcript([t.language_code for t in transcript_list])
            source_lang = transcript.language_code
            if source_lang != 'en':
                try:
                    transcript = transcript.translate('en')
                    is_translated = True
                except Exception as e:
                    logger.warning("Translation failed for %s: %s. Storing original.", video_id, e)
            else:
                source_lang = transcript.language_code
        else:
            source_lang = transcript.language_code
                
        entries = transcript.fetch()
        if not entries:
            return None, "empty transcript"
            
        text_parts = []
        for e in entries:
            part = e.get("text") if isinstance(e, dict) else getattr(e, "text", "")
            text_parts.append(part)
        text = " ".join(text_parts)
        
        text = _clean_transcript_text(text)
        
        return {
            "text": text,
            "lang": 'en' if is_translated else transcript.language_code,
            "original_lang": source_lang
        }, None
    except Exception as e:
        reason = _format_transcript_error(e)
        logger.warning("youtube-transcript-api fetch failed for %s: %s", video_id, reason)
        return None, reason


def get_transcript_with_reason(video_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Fetch transcript for a video using a multi-strategy approach.
    1. Try yt-dlp first (most resilient).
    2. Fall back to youtube-transcript-api.
    """
    logger.info("Fetching transcript for %s using yt-dlp...", video_id)
    transcript = _fetch_transcript_ytdlp(video_id)
    if transcript:
        return transcript, None
        
    logger.info("yt-dlp failed or returned empty for %s. Falling back to youtube-transcript-api...", video_id)
    return _fetch_transcript_api(video_id)


def get_transcript(video_id: str) -> Optional[Dict[str, Any]]:
    """
    Backward-compatible wrapper for callers that only need the transcript.
    Use get_transcript_with_reason when skip diagnostics matter.
    """
    transcript, _reason = get_transcript_with_reason(video_id)
    return transcript
