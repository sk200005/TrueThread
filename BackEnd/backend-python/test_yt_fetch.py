import asyncio
import sys
import logging

logging.basicConfig(level=logging.INFO)

from app.graphs.nodes.youtube_node import youtube_fetch

async def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "Python programming language"
    state = {
        "job_id": "test",
        "query": query,
        "sources": {}
    }
    
    print(f"==================================================")
    print(f"Testing YouTube fetch for query: {query}")
    print(f"==================================================")
    res = await youtube_fetch(state)
    
    yt_status = res["sources"]["youtube"]["status"]
    docs = res["sources"]["youtube"]["documents"]
    error = res["sources"]["youtube"].get("error")
    
    print(f"\nStatus: {yt_status}")
    print(f"Documents fetched: {len(docs)}")
    if error:
        print(f"Error: {error}")
    
    for doc in docs:
        print(f"\n--- {doc.get('url')} ---")
        text = doc.get("text", "")
        print(f"Content snippet: {text[:200]}...\n")

if __name__ == "__main__":
    asyncio.run(main())
