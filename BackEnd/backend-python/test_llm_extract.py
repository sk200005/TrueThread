import asyncio
import re
from app.core.llm_client import get_llm_client

async def extract_search_query(query: str) -> str:
    system_prompt = (
        "You are a search query extractor. Extract only the core Wikipedia search "
        "keywords from the user's conversational query. Remove questions, conversational "
        "words, and typos. Return ONLY the search string, no quotes, no markdown."
    )
    try:
        client = get_llm_client()
        raw = await client.chat(
            system_prompt=system_prompt,
            user_prompt=query,
            temperature=0.0,
            max_tokens=20,
        )
        cleaned = re.sub(r'^["\']|["\']$', '', raw.strip())
        return cleaned
    except Exception as e:
        print("Error:", e)
        return query

async def main():
    q = "what happend in 1987 Jammu and Kashmir Legislative Assembly election"
    print("Original:", q)
    res = await extract_search_query(q)
    print("Extracted:", res)

if __name__ == "__main__":
    asyncio.run(main())
