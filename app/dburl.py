"""DATABASE_URL normalization, shared by the app and Alembic.

Kept free of app.config so Alembic can use it without loading Settings.
"""

from __future__ import annotations


def normalize_database_url(url: str) -> str:
    """Pin bare Postgres URLs to the installed psycopg (v3) driver.

    Supabase, Railway and most hosts hand out `postgres://` or `postgresql://`, which
    SQLAlchemy maps to psycopg2 — not installed, so every service would fail
    to start. URLs that already name a driver are left alone.
    """
    for bare in ("postgres://", "postgresql://"):
        if url.startswith(bare):
            return "postgresql+psycopg://" + url[len(bare):]
    return url
