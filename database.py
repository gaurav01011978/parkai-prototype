
import sqlite3
from datetime import datetime

DB_NAME = "parkai.db"


def get_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()

    try:
        c = conn.cursor()

        # Parking lots table
        c.execute("""
            CREATE TABLE IF NOT EXISTS lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                capacity INTEGER NOT NULL,
                current_count INTEGER NOT NULL DEFAULT 0,
                lat REAL,
                lng REAL,
                last_updated TEXT
            )
        """)

        # Activity logs table
        c.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lot_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                operator TEXT,
                timestamp TEXT NOT NULL,
                undone INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Hourly parking history table
        c.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lot_id INTEGER NOT NULL,
                hour INTEGER NOT NULL,
                weekday INTEGER NOT NULL,
                occupancy_percent INTEGER NOT NULL
            )
        """)

        # Insert demo parking lots if database is empty
        count = c.execute(
            "SELECT COUNT(*) FROM lots"
        ).fetchone()[0]

        if count == 0:

            now = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            demo_lots = [
                ("Lot A", 100, 74, 28.5514, 77.2543),
                ("Lot B", 60, 52, 28.5484, 77.2536),
                ("Lot C", 150, 110, 28.5459, 77.2490),
            ]

            for name, cap, cur, lat, lng in demo_lots:

                c.execute(
                    """
                    INSERT INTO lots
                    (name, capacity, current_count, lat, lng, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (name, cap, cur, lat, lng, now)
                )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()
