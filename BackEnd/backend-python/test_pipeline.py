import asyncio
import logging
from app.graphs.research_graph import build_graph
from app.graphs.state import ResearchState

logging.basicConfig(level=logging.DEBUG)

async def main():
    graph = build_graph()
    import uuid
    initial_state: ResearchState = {
        "job_id": str(uuid.uuid4()),
        "query": "test",
        "sources_to_fetch": ["youtube"],
        "sources": {},
        "merged_documents": [],
        "status": "pending",
        "results": {},
    }
    final_state = await graph.ainvoke(initial_state)
    print("FINISHED!")

asyncio.run(main())
