"""Storage and time helpers shared by the bot and the content tools (Postgres on Neon)."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv(Path(__file__).parent / ".env")


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


TZ = ZoneInfo(os.getenv("TIMEZONE") or "Europe/Kyiv")

# posts.status: pending -> reminded -> done | skipped
SCHEMA = """
create table if not exists posts (id text primary key, subreddit text, title text, body text,
    notes text, publish_at double precision, status text default 'pending', link text,
    created double precision);
create table if not exists media (id serial primary key, post_id text references posts(id) on delete cascade,
    ord int, filename text, data bytea);
create table if not exists kv (key text primary key, value text);
"""


def q(sql: str, params: tuple | None = None, one: bool = False):
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL missing (Postgres, e.g. a free Neon database).")
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
        cur = conn.execute(sql, params)
        if cur.description is None:
            return None
        return cur.fetchone() if one else cur.fetchall()


def init_db() -> None:
    q(SCHEMA)


def kv_get(key: str) -> Optional[str]:
    row = q("select value from kv where key=%s", (key,), one=True)
    return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    q("insert into kv (key, value) values (%s, %s) on conflict (key) do update set value=excluded.value",
      (key, value))


def now() -> datetime:
    return datetime.now(TZ)


def fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=TZ).strftime("%d.%m %H:%M")


def parse_when(s: str) -> float:
    """'18:30' -> next such time; '27.10 18:30' or '2026-10-27 18:30'; 'зараз' -> now (TIMEZONE)."""
    s = s.strip().lower()
    cur = now()
    if s in ("", "now", "зараз"):
        return cur.timestamp()
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        h, m = map(int, s.split(":"))
        when = cur.replace(hour=h, minute=m, second=0, microsecond=0)
        return (when if when > cur else when + timedelta(days=1)).timestamp()
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+(\d{1,2}):(\d{2})", s)
    if m:
        d, mo, y, h, mi = m.groups()
        return datetime(int(y or cur.year), int(mo), int(d), int(h), int(mi), tzinfo=TZ).timestamp()
    when = datetime.fromisoformat(s.replace("t", " "))
    return (when if when.tzinfo else when.replace(tzinfo=TZ)).timestamp()


def spacing_warnings(subreddit: str, ts: float, exclude_id: str = "") -> list[str]:
    """Anti-ban hints: same subreddit within 24 h, too many posts that day."""
    out = []
    same = q("select count(*) as n from posts where lower(subreddit)=lower(%s) and status<>'skipped' "
             "and abs(publish_at-%s)<86400 and id<>%s", (subreddit, ts, exclude_id), one=True)["n"]
    if same:
        out.append(f"у r/{subreddit} вже є пост у межах 24 год")
    day = datetime.fromtimestamp(ts, tz=TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    n = q("select count(*) as n from posts where status<>'skipped' and publish_at>=%s and publish_at<%s "
          "and id<>%s", (day, day + 86400, exclude_id), one=True)["n"]
    if n >= 2:
        out.append(f"цього дня вже {n} пости, краще не більше 2")
    return out
