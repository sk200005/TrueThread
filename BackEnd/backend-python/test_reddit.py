import asyncio
from app.ingestion.reddit_client import scrape

async def main():
    query = "best laptops under 70k"
    print(f"Scraping reddit for: {query}")
    results = await scrape(query)
    
    print(f"Found {len(results)} posts.")
    for post in results:
        print(f"Post: {post.get('title', 'Unknown')} ({post.get('upvotes', '0')} upvotes)")
        print(f"Subreddit: {post.get('subreddit')}")
        print(f"URL: {post.get('url')}")
        print(f"Body length: {len(post.get('body', ''))}")
        comments = post.get('comments', [])
        print(f"Comments: {len(comments)}")
        if comments:
            print(f"Top comment: {comments[0].get('text', '')[:50]}...")
        print("-" * 40)

if __name__ == "__main__":
    asyncio.run(main())
