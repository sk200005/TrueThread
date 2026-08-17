import asyncio
import json
import uuid
import logging
from app.core.database import async_session
from sqlalchemy import text

logging.basicConfig(level=logging.INFO)

async def main():
    async with async_session() as session:
        try:
            doc_id = uuid.uuid4()
            eng_metrics = {"views": 100, "likes": 10, "duration": "10:00"}
            eng_metrics_json = json.dumps(eng_metrics)
            metadata = {"description": "x" * 300, "thumbnail_url": "url"}
            metadata_json = json.dumps(metadata)

            await session.execute(
                text("""
                    INSERT INTO source_documents (id, source, author, title, text, url, published_at, engagement_metrics, metadata, created_at)
                    VALUES (:id, :source, :author, :title, :text, :url, :published_at, :engagement_metrics, :metadata, now())
                """),
                {
                    "id": str(doc_id),
                    "source": "youtube",
                    "author": "Author",
                    "title": "YouTube Title",
                    "text": "YouTube Text",
                    "url": "http://youtube.com",
                    "published_at": None,
                    "engagement_metrics": eng_metrics_json,
                    "metadata": metadata_json,
                },
            )
            await session.commit()
            print("Success")
        except Exception as e:
            print("Error:", str(e))

asyncio.run(main())
