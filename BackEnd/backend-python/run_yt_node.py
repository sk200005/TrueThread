import asyncio
from app.graphs.nodes.youtube_node import youtube_fetch
from app.graphs.state import ResearchState

async def main():
    state = {"query": "best laptops under 70k", "sources": {}}
    res = await youtube_fetch(state)
    print("Result:", res)

if __name__ == "__main__":
    asyncio.run(main())
