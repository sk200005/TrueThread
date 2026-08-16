import asyncio
from app.graphs.nodes.wikipedia_node import wikipedia_fetch
from app.graphs.nodes.youtube_node import youtube_fetch

async def main():
    state = {
        "job_id": "test",
        "query": "Python programming language",
        "sources": {}
    }
    print("Testing wikipedia...")
    res = await wikipedia_fetch(state)
    print("Wiki result:", len(res["sources"]["wikipedia"]["documents"]))
    
    print("Testing youtube...")
    res2 = await youtube_fetch(state)
    print("Youtube result:", len(res2["sources"]["youtube"]["documents"]))

if __name__ == "__main__":
    asyncio.run(main())
