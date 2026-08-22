import asyncio
from app.core.database import async_session
from sqlalchemy import text

async def main():
    async with async_session() as session:
        result = await session.execute(text("""
            SELECT q.id, q.user_id, q.query_text, q.status,
                   u.email
            FROM queries q
            LEFT JOIN users u ON u.id = q.user_id
            ORDER BY q.created_at DESC LIMIT 5
        """))
        print("=== QUERIES WITH USER INFO ===")
        for row in result.fetchall():
            print(f"query_id={row[0]} | user_id={row[1]} | email={row[4]} | status={row[3]} | text={row[2]}")
        
        # Also show all users
        result = await session.execute(text("SELECT id, email FROM users"))
        print("\n=== ALL USERS ===")
        for row in result.fetchall():
            print(f"  id={row[0]} | email={row[1]}")

asyncio.run(main())
