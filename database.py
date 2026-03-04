import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "prices.db"


def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prices (
                id        INTEGER PRIMARY KEY,
                model_name TEXT,
                url       TEXT,
                price     REAL,
                shop      TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def insert_price(model_name: str, url: str, price: float, shop: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO prices (model_name, url, price, shop) VALUES (?, ?, ?, ?)",
            (model_name, url, price, shop),
        )
        conn.commit()


def get_latest_prices() -> list[dict]:
    """Return the most recent price row for every distinct model_name."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT model_name, url, price, shop, timestamp
            FROM prices
            WHERE id IN (
                SELECT MAX(id) FROM prices GROUP BY model_name
            )
            ORDER BY model_name
        """).fetchall()
    return [
        {"model_name": r[0], "url": r[1], "price": r[2], "shop": r[3], "timestamp": r[4]}
        for r in rows
    ]


def get_price_7_days_ago(model_name: str) -> dict | None:
    """Return the closest price record from 6–8 days ago for a given model."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT price, shop, timestamp
            FROM prices
            WHERE model_name = ?
              AND timestamp <= datetime('now', '-6 days')
            ORDER BY timestamp DESC
            LIMIT 1
        """, (model_name,)).fetchone()
    if row:
        return {"price": row[0], "shop": row[1], "timestamp": row[2]}
    return None
