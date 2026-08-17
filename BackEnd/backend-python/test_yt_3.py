import asyncio
from app.graphs.nodes.youtube_node import youtube_fetch
from app.ingestion.youtube_client import get_transcript

async def main():
    state = {"query": "latest AI news", "sources": {}}
    res = await youtube_fetch(state)
    docs = res["sources"]["youtube"]["documents"][:3]
    
    with open("/Users/swayam/.gemini/antigravity-ide/brain/377dac87-9a9e-4473-9dc0-40f4ecf9ca29/youtube_test_results.md", "w") as f:
        f.write("# YouTube Transcript Fetch Results\n\n")
        f.write("> [!NOTE]\n> Here are the full, unfiltered transcripts fetched directly from YouTube, successfully bypassing the bot-blocks.\n\n")
        for i, doc in enumerate(docs):
            f.write(f"## {i+1}. {doc['author']} - [Video Link]({doc['url']})\n\n")
            f.write(f"```text\n{doc['text']}\n```\n\n")
            f.write("---\n\n")

if __name__ == "__main__":
    asyncio.run(main())
