import asyncio
from langgraph.graph import StateGraph, START, END
from typing import TypedDict, Annotated
import operator

class State(TypedDict):
    a: str
    b: str
    c: str

def node_a(state: State):
    print("Node A")
    return {"a": "A"}

def node_b(state: State):
    print("Node B")
    return {"b": "B"}

def node_c(state: State):
    print("Node C")
    return {"c": "C"}

graph = StateGraph(State)
graph.add_node("A", node_a)
graph.add_node("B", node_b)
graph.add_node("C", node_c)

graph.add_edge(START, "A")
graph.add_edge(START, "B")
graph.add_edge("A", "C")
graph.add_edge("B", "C")
graph.add_edge("C", END)

app = graph.compile()
print(app.invoke({}))
