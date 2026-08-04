import asyncio
import logging
import sys
from dotenv import load_dotenv

# Load env variables before importing modules
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_phase_e")

from app.graphs.nodes.classify_claim import classify_claims
from app.graphs.nodes.news_verify import news_verify
from app.graphs.nodes.wiki_verify import wiki_verify
from app.graphs.nodes.verify_claim import verify_claim
from app.graphs.nodes.summarize import summarize
from app.graphs.query_graph import build_query_graph
from app.graphs.state import QueryState, ExtractedClaimDict


async def test_claim_classifier():
    print("\n" + "="*80)
    print("TEST 1: Claim Classifier (Routing Edge)")
    print("="*80)

    claims = [
        {
            "claim_text": "The iPhone 16 Pro has widespread overheating issues during video recording.",
            "entities": ["iPhone 16 Pro", "overheating"],
            "claim_type": "factual",
            "confidence": "high",
            "source_comment_id": "comment_1"
        },
        {
            "claim_text": "The Wright brothers made their first successful airplane flight in 1903.",
            "entities": ["Wright brothers", "airplane flight"],
            "claim_type": "factual",
            "confidence": "high",
            "source_comment_id": "comment_2"
        },
        {
            "claim_text": "I think the iPhone 16 Pro is the most beautiful phone ever made and everyone should buy it.",
            "entities": ["iPhone 16 Pro"],
            "claim_type": "opinion",
            "confidence": "medium",
            "source_comment_id": "comment_3"
        }
    ]

    state: QueryState = {
        "extracted_claims": claims
    }

    res = await classify_claims(state)
    classified = res.get("classified_claims", [])

    print(f"\nExtracted claims: {len(claims)}")
    print(f"Classified claims: {len(classified)}")

    for c in classified:
        print(f"\nClaim: {c['claim_text']}")
        print(f"  Verifiable: {c['verifiable']}")
        print(f"  Time Nature: {c['time_nature']}")
        print(f"  Route: {c['route']}")

    return classified


async def test_verification_scoring_agreeing():
    print("\n" + "="*80)
    print("TEST 2: Verification Scorer — Agreeing Sources (Supported)")
    print("="*80)

    claim = {
        "claim_text": "The Wright brothers made their first successful airplane flight in 1903.",
        "entities": ["Wright brothers", "first successful airplane flight"],
        "claim_type": "factual",
        "confidence": "high",
        "source_comment_id": "comment_2",
        "route": "both"
    }

    news_ev = {
        "claim_text": claim["claim_text"],
        "route": "both",
        "news_articles": [
            {
                "title": "Wright Brothers Historic Flight Remembered",
                "url": "https://example.com/news/wright",
                "source_name": "History News",
                "snippet": "In December 1903, Wilbur and Orville Wright made the first sustained, controlled flight of a powered aircraft.",
                "published_at": "2026-01-01T00:00:00Z",
                "relevance_score": 0.95
            }
        ]
    }

    wiki_ev = {
        "claim_text": claim["claim_text"],
        "route": "both",
        "wiki_context": "The Wright brothers, Orville and Wilbur, were American aviation pioneers generally credited with inventing, building, and flying the world's first successful motor-operated airplane. They made the first controlled, sustained flight of a powered aircraft on December 17, 1903.",
        "wiki_url": "https://en.wikipedia.org/wiki/Wright_brothers",
        "wiki_title": "Wright brothers"
    }

    state: QueryState = {
        "classified_claims": [claim],
        "news_evidence": [news_ev],
        "wiki_evidence": [wiki_ev]
    }

    res = await verify_claim(state)
    verified = res.get("verified_claims", [])

    for c in verified:
        print(f"Verdict: {c['verdict']}")
        print(f"Confidence: {c['confidence']}")
        print(f"Source Type: {c['source_type']}")
        print(f"Citations: {c['citations']}")
        print(f"Justification: {c['justification']}")


async def test_verification_scoring_disputed():
    print("\n" + "="*80)
    print("TEST 3: Verification Scorer — Conflicting Sources (Disputed)")
    print("="*80)

    claim = {
        "claim_text": "Scientists confirmed that a massive asteroid will hit Earth next Tuesday.",
        "entities": ["asteroid", "hit Earth"],
        "claim_type": "factual",
        "confidence": "high",
        "source_comment_id": "comment_4",
        "route": "both"
    }

    # News article says it's a false rumor
    news_ev = {
        "claim_text": claim["claim_text"],
        "route": "both",
        "news_articles": [
            {
                "title": "NASA Debunks Asteroid Collision Rumors",
                "url": "https://example.com/news/asteroid-fake",
                "source_name": "Science Today",
                "snippet": "NASA scientists released a statement yesterday debunking social media rumors claiming a massive asteroid will collide with Earth next Tuesday. Calculations show it will miss by millions of miles.",
                "published_at": "2026-08-01T00:00:00Z",
                "relevance_score": 0.98
            }
        ]
    }

    # Simulated wiki page says it WILL hit (maybe an edit war or historical scenario)
    wiki_ev = {
        "claim_text": claim["claim_text"],
        "route": "both",
        "wiki_context": "The asteroid Bennu-99 is a near-Earth object. Standard scientific models published by major astronomical groups in 2026 confirm that Bennu-99 will impact the Earth's surface next Tuesday, causing a major extinction event.",
        "wiki_url": "https://en.wikipedia.org/wiki/Fictional_Asteroid_Impact",
        "wiki_title": "Asteroid Bennu-99 Impact"
    }

    state: QueryState = {
        "classified_claims": [claim],
        "news_evidence": [news_ev],
        "wiki_evidence": [wiki_ev]
    }

    res = await verify_claim(state)
    verified = res.get("verified_claims", [])

    for c in verified:
        print(f"Verdict: {c['verdict']}")
        print(f"Confidence: {c['confidence']}")
        print(f"Source Type: {c['source_type']}")
        print(f"Citations: {c['citations']}")
        print(f"Justification: {c['justification']}")


async def test_end_to_end_graph():
    print("\n" + "="*80)
    print("TEST 4: End-to-End Query Graph Execution")
    print("="*80)

    # We patch the retrieve node so it doesn't search DB but uses our pre-populated chunks
    import app.graphs.query_graph as query_graph_mod
    async def mock_retrieve(state):
        return {"retrieved_chunks": state.get("retrieved_chunks", [])}
    query_graph_mod.rag_retrieve = mock_retrieve

    # We build the graph
    graph = build_query_graph()

    # Pre-populate retrieve_chunks to simulate RAG retrieve output
    retrieved_chunks = [
        {
            "chunk_id": "chunk_1",
            "chunk_text": "I purchased the new iPhone 16 Pro yesterday. However, it seems to have major overheating issues. Whenever I record 4K video, the phone gets extremely hot to touch and drops frames.",
            "source_document_id": "doc_1",
            "similarity": 0.89
        },
        {
            "chunk_id": "chunk_2",
            "chunk_text": "The history of flight is fascinating. Many believe that the Wright brothers were the first. Orville and Wilbur Wright achieved flight in December 1903 at Kitty Hawk.",
            "source_document_id": "doc_2",
            "similarity": 0.85
        },
        {
            "chunk_id": "chunk_3",
            "chunk_text": "I think Android is way better than iOS. iPhone is overpriced and boring. Apple has lost its innovation.",
            "source_document_id": "doc_3",
            "similarity": 0.75
        }
    ]

    initial_state: QueryState = {
        "query": "iPhone overheating and flight history",
        "retrieved_chunks": retrieved_chunks,
        "top_k": 3
    }

    print("Invoking graph...")
    res = await graph.ainvoke(initial_state)

    print("\nExecution complete.")
    print("Extracted claims:")
    for claim in res.get("extracted_claims", []):
        print(f"  - [{claim.get('claim_type')}] {claim['claim_text']}")

    print("\nClassified claims:")
    for claim in res.get("classified_claims", []):
        print(f"  - Route [{claim.get('route')}]: {claim['claim_text']}")

    print("\nVerified claims:")
    for claim in res.get("verified_claims", []):
        print(f"  - Verdict [{claim['verdict']}] (conf={claim['confidence']}): {claim['claim']}")
        print(f"    Justification: {claim['justification']}")
        print(f"    Citations: {claim['citations']}")

    print("\nFinal report summary:")
    print(res.get("final_report", {}).get("summary"))


async def main():
    await test_claim_classifier()
    await test_verification_scoring_agreeing()
    await test_verification_scoring_disputed()
    await test_end_to_end_graph()


if __name__ == "__main__":
    asyncio.run(main())
