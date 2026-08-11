import asyncio
from langgraph.graph import StateGraph, START, END
from typing import TypedDict, Annotated, Literal
import operator

def merge_sources(a: dict, b: dict) -> dict:
    res = a.copy()
    for k, v in b.items():
        if k in res:
            res[k] = {**res[k], **v}
        else:
            res[k] = v
    return res

class State(TypedDict, total=False):
    sources: Annotated[dict, merge_sources]

async def node_a(state: State):
    await asyncio.sleep(1)
    print("Node A done")
    return {"sources": {"a": {"status": "done"}}}

async def node_reddit(state: State):
    try:
        raise RuntimeError("fail")
    except Exception as exc:
        print("Node Reddit done")
        return {"sources": {"reddit": {"status": "failed", "error": str(exc)}}}

async def store(state: State):
    print("Store running")
    return {"sources": {}}

graph = StateGraph(State)
graph.add_node("A", node_a)
graph.add_node("Reddit", node_reddit)
graph.add_node("Store", store)

graph.add_edge(START, "A")
graph.add_edge(START, "Reddit")
graph.add_edge("A", "Store")
graph.add_edge("Reddit", "Store")
graph.add_edge("Store", END)

app = graph.compile()
print(asyncio.run(app.ainvoke({"sources": {}})))
