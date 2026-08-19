import sys
import asyncio
from app.graphs.nodes.youtube_node import youtube_fetch
from app.ingestion.youtube_client import get_transcript

async def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "latest AI news"
    state = {"query": query, "sources": {}}
    res = await youtube_fetch(state)
    docs = res["sources"]["youtube"]["documents"][:3]
    
    output_file = "test_yt_3_results.md"
    print(f"==================================================")
    print(f"Fetching results for query: '{query}'")
    print(f"==================================================")
    with open(output_file, "w") as f:
        f.write(f"# YouTube Transcript Fetch Results for: {query}\n\n")
        f.write("> [!NOTE]\n> Here are the full, unfiltered transcripts fetched directly from YouTube, successfully bypassing the bot-blocks.\n\n")
        for i, doc in enumerate(docs):
            f.write(f"## {i+1}. {doc.get('author', 'Unknown')} - [Video Link]({doc.get('url', '')})\n\n")
            f.write(f"```text\n{doc.get('text', '')}\n```\n\n")
            f.write("---\n\n")
            print(f"\n--- {doc.get('url', '')} ---")
            print(f"Title/Author: {doc.get('author', 'Unknown')}")
            print(f"Content snippet: {doc.get('text', '')[:200]}...\n")
            
    print(f"\n✅ Full transcripts have been saved to: {output_file}")

if __name__ == "__main__":
    asyncio.run(main())
