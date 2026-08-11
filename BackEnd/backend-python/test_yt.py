import asyncio
from app.ingestion.youtube_client import search_videos, get_video_metadata, get_transcript, get_top_comments
from app.core.config import settings

async def main():
    print(f"Key: {settings.youtube_api_key}")
    q = "best laptops under 70k"
    vids = await search_videos(q, 3)
    print("Videos:", vids)
    
    meta = await get_video_metadata(vids, 2)
    print("Meta count:", len(meta))
    
    for v in meta:
        print("Meta:", v["title"], v["views"])
        t = await asyncio.to_thread(get_transcript, v["videoId"])
        if t:
            print("Transcript text len:", len(t["text"]))
            print("Transcript lang:", t["lang"])
        else:
            print("No transcript")
            
        c = await get_top_comments(v["videoId"], 2)
        print("Comments:", len(c))

if __name__ == "__main__":
    asyncio.run(main())
