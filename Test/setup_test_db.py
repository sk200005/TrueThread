"""
setup_test_db.py — Create/reset the 'research_test' database for pipeline testing.

Usage:
    python setup_test_db.py          # Create database + schema (if not exists)
    python setup_test_db.py --reset  # Drop and recreate (clean slate)
"""

import argparse
import sys
import os

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

# ── Connection settings (matches docker-compose.yml) ──────────────────────
DB_HOST = "localhost"
DB_PORT = 5433
DB_USER = "swayam"
DB_PASSWORD = "PGSQLpw#1"
TEST_DB_NAME = "research_test"

# Path to the main schema file
SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "BackEnd", "schema.sql"
)


def get_admin_connection():
    """Connect to the default 'postgres' database for admin operations."""
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname="postgres",
        user=DB_USER,
        password=DB_PASSWORD,
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def database_exists(cursor, db_name):
    """Check if a database exists."""
    cursor.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
    )
    return cursor.fetchone() is not None


def create_database(reset=False):
    """Create the test database and apply schema."""
    conn = get_admin_connection()
    cursor = conn.cursor()

    try:
        exists = database_exists(cursor, TEST_DB_NAME)

        if exists and reset:
            print(f"  ⚠  Dropping existing database '{TEST_DB_NAME}'...")
            # Terminate existing connections
            cursor.execute(f"""
                SELECT pg_terminate_backend(pg_stat_activity.pid)
                FROM pg_stat_activity
                WHERE pg_stat_activity.datname = '{TEST_DB_NAME}'
                AND pid <> pg_backend_pid()
            """)
            cursor.execute(f"DROP DATABASE {TEST_DB_NAME}")
            exists = False
            print(f"  ✅ Database dropped.")

        if not exists:
            print(f"  → Creating database '{TEST_DB_NAME}'...")
            cursor.execute(f"CREATE DATABASE {TEST_DB_NAME}")
            print(f"  ✅ Database created.")
        else:
            print(f"  ℹ  Database '{TEST_DB_NAME}' already exists.")

    finally:
        cursor.close()
        conn.close()

    # Apply schema
    apply_schema()


def apply_schema():
    """Apply schema.sql to the test database."""
    if not os.path.exists(SCHEMA_PATH):
        print(f"  ❌ Schema file not found: {SCHEMA_PATH}")
        sys.exit(1)

    with open(SCHEMA_PATH, "r") as f:
        schema_sql = f.read()

    print(f"  → Applying schema from {os.path.basename(SCHEMA_PATH)}...")

    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=TEST_DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cursor = conn.cursor()

    try:
        cursor.execute(schema_sql)
        print("  ✅ Schema applied successfully.")

        # Verify tables
        cursor.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        tables = [row[0] for row in cursor.fetchall()]
        print(f"  📋 Tables: {', '.join(tables)}")

    except Exception as e:
        print(f"  ❌ Schema error: {e}")
        sys.exit(1)
    finally:
        cursor.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Setup the research_test database for pipeline testing."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate the database (clean slate)",
    )
    args = parser.parse_args()

    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║          TEST DATABASE SETUP                     ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"  Host: {DB_HOST}:{DB_PORT}")
    print(f"  Database: {TEST_DB_NAME}")
    print(f"  Reset: {args.reset}")
    print()

    try:
        create_database(reset=args.reset)
    except psycopg2.OperationalError as e:
        print(f"  ❌ Cannot connect to PostgreSQL: {e}")
        print(f"  💡 Make sure Docker is running: docker compose up -d")
        sys.exit(1)

    print()
    print("  🎉 Test database is ready!")
    print(f"  Connection: postgresql://swayam:***@{DB_HOST}:{DB_PORT}/{TEST_DB_NAME}")
    print()


if __name__ == "__main__":
    main()
