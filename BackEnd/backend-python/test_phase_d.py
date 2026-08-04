import asyncio
import logging
import uuid
import sys

# Configure logging to see the timestamps
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_runner")

from app.graphs.research_graph import build_graph, ResearchState

# Patch the reddit fetch node to sleep for a bit to prove concurrency
import app.graphs.nodes.reddit_node as reddit_node
import app.graphs.nodes.youtube_node as youtube_node
import app.graphs.nodes.wikipedia_node as wikipedia_node

original_reddit_fetch = reddit_node.reddit_fetch
async def patched_reddit_fetch(state: ResearchState):
    logger.info("TEST: reddit_fetch starting, sleeping 1s...")
    await asyncio.sleep(1)
    # Simulate failure if we want
    if state.get("query") == "fail_reddit":
        logger.error("TEST: SIMULATED REDDIT FAILURE")
        return {
            "sources": {
                "reddit": {
                    "status": "failed",
                    "documents": [],
                    "error": "Simulated failure"
                }
            }
        }
    res = await original_reddit_fetch(state)
    logger.info("TEST: reddit_fetch done.")
    return res

original_youtube_fetch = youtube_node.youtube_fetch
async def patched_youtube_fetch(state: ResearchState):
    logger.info("TEST: youtube_fetch starting, sleeping 2s...")
    await asyncio.sleep(2)
    res = await original_youtube_fetch(state)
    logger.info("TEST: youtube_fetch done.")
    return res

original_wikipedia_fetch = wikipedia_node.wikipedia_fetch
async def patched_wikipedia_fetch(state: ResearchState):
    logger.info("TEST: wikipedia_fetch starting...")
    res = await original_wikipedia_fetch(state)
    logger.info("TEST: wikipedia_fetch done.")
    return res

reddit_node.reddit_fetch = patched_reddit_fetch
youtube_node.youtube_fetch = patched_youtube_fetch
wikipedia_node.wikipedia_fetch = patched_wikipedia_fetch

# We don't want to actually hit the DB for storing, so patch store_documents
import app.graphs.nodes.store_node as store_node
async def mock_store_documents(state: ResearchState):
    merged = []
    sources = state.get("sources", {})
    for src, res in sources.items():
        if res.get("status") == "done":
            merged.extend(res.get("documents", []))
    
    logger.info("TEST: store_documents merged %d documents total", len(merged))
    return {"merged_documents": merged, "status": "storing"}

store_node.store_documents = mock_store_documents


async def run_test_1():
    print("\n\n" + "="*80)
    print("TEST 1: Fresh run with all three sources succeeding")
    print("="*80)
    
    graph = build_graph()
    state = {
        "job_id": str(uuid.uuid4()),
        "query": "artificial intelligence",
        "sources_to_fetch": ["wikipedia", "reddit", "youtube"],
        "sources": {},
        "merged_documents": [],
        "status": "pending",
        "results": {}
    }
    
    res = await graph.ainvoke(state)
    merged = res.get("merged_documents", [])
    print(f"\n[Test 1 Result] Merged documents count: {len(merged)}")
    for s in ["wikipedia", "reddit", "youtube"]:
        print(f"  {s} status: {res.get('sources', {}).get(s, {}).get('status')}")


async def run_test_2():
    print("\n\n" + "="*80)
    print("TEST 2: Simulate Reddit failure")
    print("="*80)
    
    graph = build_graph()
    state = {
        "job_id": str(uuid.uuid4()),
        "query": "fail_reddit", # Triggers our mock failure
        "sources_to_fetch": ["wikipedia", "reddit", "youtube"],
        "sources": {},
        "merged_documents": [],
        "status": "pending",
        "results": {}
    }
    
    res = await graph.ainvoke(state)
    merged = res.get("merged_documents", [])
    print(f"\n[Test 2 Result] Merged documents count: {len(merged)}")
    for s in ["wikipedia", "reddit", "youtube"]:
        print(f"  {s} status: {res.get('sources', {}).get(s, {}).get('status')}")
    
    return res.get('sources', {})


async def run_test_3(prior_sources):
    print("\n\n" + "="*80)
    print("TEST 3: Retry after Test 2 failure")
    print("="*80)
    
    # Identify which sources failed
    failed_sources = [s for s, res in prior_sources.items() if res.get("status") == "failed"]
    print(f"Sources to retry (failed in Test 2): {failed_sources}")
    
    # Pre-populate state for unrequested (already done) sources
    all_possible = ["wikipedia", "reddit", "youtube"]
    initial_sources = {}
    for s in all_possible:
        if s not in failed_sources:
            initial_sources[s] = {"status": "done", "documents": [], "error": None}
            
    graph = build_graph()
    state = {
        "job_id": str(uuid.uuid4()),
        "query": "artificial intelligence", # No longer fail_reddit
        "sources_to_fetch": failed_sources,
        "sources": initial_sources,
        "merged_documents": [],
        "status": "pending",
        "results": {}
    }
    
    res = await graph.ainvoke(state)
    merged = res.get("merged_documents", [])
    print(f"\n[Test 3 Result] Merged documents count: {len(merged)}")
    for s in ["wikipedia", "reddit", "youtube"]:
        print(f"  {s} status: {res.get('sources', {}).get(s, {}).get('status')}")


async def main():
    await run_test_1()
    prior_sources = await run_test_2()
    await run_test_3(prior_sources)

if __name__ == "__main__":
    asyncio.run(main())
