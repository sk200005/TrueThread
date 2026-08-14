import asyncio
from sqlalchemy import text
from app.core.database import async_session

async def test():
    async with async_session() as session:
        # test implicit begin with begin_nested
        async with session.begin_nested():
            await session.execute(text("CREATE TABLE IF NOT EXISTS test_persist (id serial);"))
            await session.execute(text("INSERT INTO test_persist DEFAULT VALUES;"))
        
        await session.commit()
        
    # Check if it persisted
    async with async_session() as session2:
        result = await session2.execute(text("SELECT count(*) FROM test_persist;"))
        count = result.scalar()
        print(f"Count after commit: {count}")

asyncio.run(test())
