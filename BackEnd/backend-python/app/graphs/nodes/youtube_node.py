"""
graphs/nodes/youtube_node.py — Fetch YouTube content for a research query.
"""

from __future__ import annotations

import logging
import asyncio
from typing import Any
from datetime import datetime, timezone

from app.graphs.state import ResearchState, SourceDoc
from app.ingestion import youtube_client

logger = logging.getLogger(__name__)

# Constants for how many videos to process
TARGET_COUNT = 5

async def youtube_fetch(state: ResearchState) -> dict[str, Any]:
    """
    LangGraph node: fetch YouTube transcripts for the query.

    Populates state["sources"]["youtube"] with SourceDoc dicts.
    """
    query = state.get("query", "")
    sources = state.get("sources", {})
    youtube_state = sources.get("youtube", {})

    if youtube_state.get("status") == "done":
        logger.info("YouTube fetch already done, skipping.")
        return {"sources": {"youtube": youtube_state}}

    logger.info("YouTube fetch starting for query: %r", query)
    documents: list[SourceDoc] = []

    try:
        # 1. Search videos
        video_ids = await youtube_client.search_videos(query, max_results=20)
        
        # 2. Get ranked metadata
        ranked_videos = await youtube_client.get_video_metadata(video_ids, target_count=TARGET_COUNT)
        
        # 3. Process candidates in order
        collected = 0
        for video in ranked_videos:
            if collected >= TARGET_COUNT:
                break
                
            video_id = video["videoId"]
            logger.info("Processing YouTube candidate: %s - %s", video_id, video["title"])
            
            # Since get_transcript is synchronous, we run it in the default executor
            loop = asyncio.get_running_loop()
            transcript_obj = await loop.run_in_executor(None, youtube_client.get_transcript, video_id)
            
            if not transcript_obj:
                logger.info("Skipping %s - no transcript available.", video_id)
                continue
                
            # Get top comments
            comments = await youtube_client.get_top_comments(video_id, max_results=3)
            
            # Format text
            text_parts = [
                f"Title: {video['title']}",
                f"Description: {video['description']}",
                f"Transcript:\n{transcript_obj['text']}",
            ]
            if comments:
                comments_text = "\n".join([f"- {c['author']}: {c['text']}" for c in comments])
                text_parts.append(f"Comments:\n{comments_text}")
                
            full_text = "\n\n".join(text_parts)
            
            # Parse publishedAt to ISO format or None if missing
            pub_date = None
            if video["publishedAt"]:
                # Ensure it's valid ISO 8601, youtube returns e.g. 2021-03-12T14:32:00Z
                try:
                    # just keep as string for datetime parsing later if needed
                    pub_date = datetime.fromisoformat(video["publishedAt"].replace('Z', '+00:00'))
                except ValueError:
                    pass

            doc: SourceDoc = {
                "source": "youtube",
                "author": video["channel"],
                "text": full_text,
                "url": video["url"],
                "published_at": pub_date,
                "engagement_metrics": {
                    "views": video["views"],
                    "likes": video["likes"],
                    "duration": video["duration"]
                },
            }
            documents.append(doc)
            collected += 1

        if not documents:
            logger.warning("No YouTube transcripts were successfully collected.")
        else:
            logger.info("Successfully fetched %d YouTube transcripts.", len(documents))

    except Exception as exc:
        logger.exception("YouTube fetch failed entirely: %s", exc)
        return {
            "sources": {
                "youtube": {
                    "status": "failed",
                    "documents": [],
                    "error": str(exc)
                }
            }
        }

    return {
        "sources": {
            "youtube": {
                "status": "done",
                "documents": documents,
                "error": None
            }
        }
    }
