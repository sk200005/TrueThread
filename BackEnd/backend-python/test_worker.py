import asyncio
from app.worker import process_research_job

class MockJob:
    def __init__(self):
        self.id = "test_job_id"
        self.data = {
            "jobId": "test_job_id",
            "queryText": "test",
            "sources": ["wikipedia", "reddit", "youtube"]
        }
    async def updateProgress(self, evt):
        print("Progress:", evt)

async def main():
    job = MockJob()
    print("Starting job")
    try:
        res = await process_research_job(job, "token")
        print("Job finished:", res)
    except Exception as e:
        print("Job crashed:", e)

asyncio.run(main())
