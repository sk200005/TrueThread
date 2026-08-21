
import sys
import asyncio
from app.ingestion.youtube_client import search_videos, get_video_metadata, get_transcript, get_top_comments
from app.core.config import settings

async def main():
    q = sys.argv[1] if len(sys.argv) > 1 else "best laptops under 70k"
    print(f"==================================================")
    print(f"Searching for: '{q}'")
    print(f"==================================================")
    
    vids = await search_videos(q, 3)
    print("Video IDs Found:", vids)
    
    meta = await get_video_metadata(vids, 2)
    print(f"Metadata count: {len(meta)}")
    
    for v in meta:
        print(f"\n--- {v.get('title')} ---")
        print(f"Views: {v.get('views')} | Video ID: {v.get('videoId')}")
        
        t = await asyncio.to_thread(get_transcript, v["videoId"])
        if t:
            print(f"Transcript lang: {t['lang']}, Length: {len(t['text'])} chars")
            print(f"Transcript Snippet: {t['text'][:200]}...\n")
        else:
            print("No transcript available.\n")
            
        c = await get_top_comments(v["videoId"], 2)
        print(f"Top Comments ({len(c)} fetched):")
        for i, comment in enumerate(c):
            # Safe access to text in case the format is different
            text = comment.get('text', '') if isinstance(comment, dict) else str(comment)
            print(f" {i+1}. {text[:100]}...")

if __name__ == "__main__":
    asyncio.run(main())
