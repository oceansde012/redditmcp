"""Shared Reddit logic for the MCP server and the cloud worker.

Reddit client, Postgres storage, safety limits, posting, editing and the post queue.
Every write goes through SafetyGuard (pauses, daily caps, duplicate-text check).
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import praw
import psycopg
from dotenv import load_dotenv
from prawcore.exceptions import PrawcoreException
from psycopg.rows import dict_row

load_dotenv(Path(__file__).parent / ".env")


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


MIN_GAP_COMMENT = env_int("MIN_SECONDS_BETWEEN_COMMENTS", 180)
MIN_GAP_POST = env_int("MIN_SECONDS_BETWEEN_POSTS", 1800)
MIN_GAP_SAME_SUB_POST = env_int("MIN_SECONDS_SAME_SUBREDDIT_POST", 86400)
MAX_COMMENTS_DAY = env_int("MAX_COMMENTS_PER_DAY", 30)
MAX_POSTS_DAY = env_int("MAX_POSTS_PER_DAY", 5)
REPLY_FOOTER = os.getenv("REPLY_FOOTER", "")
TZ = ZoneInfo(os.getenv("TIMEZONE") or "Europe/Kyiv")


class Blocked(Exception):
    """A write action was refused by the safety limits; it can be retried later."""


# ------------------------------------------------------------- database ---

SCHEMA = """
create table if not exists actions (id serial primary key, kind text, ts double precision,
    hash text, thing_id text, subreddit text);
create table if not exists scheduled (id text primary key, subreddit text, title text, body text,
    url text, flair_id text, publish_at double precision, status text, link text, error text);
create table if not exists drafts (id text primary key, kind text, target text, context text,
    body text, status text, created double precision, tg_message_id bigint, result text);
create table if not exists seen (id text primary key, ts double precision);
create table if not exists kv (key text primary key, value text);
"""


def q(sql: str, params: tuple | None = None, one: bool = False):
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL missing in .env (Postgres, e.g. a free Neon database).")
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


# --------------------------------------------------------------- helpers ---

def fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=TZ).strftime("%d.%m.%Y %H:%M")


def parse_when(s: str) -> Optional[float]:
    """'now'/'зараз' -> None; '18:30' -> next such time; '2026-10-27 18:30' -> that time (TIMEZONE)."""
    s = s.strip().lower()
    if s in ("", "now", "зараз", "одразу"):
        return None
    now = datetime.now(TZ)
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        h, m = map(int, s.split(":"))
        when = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return (when if when > now else when + timedelta(days=1)).timestamp()
    when = datetime.fromisoformat(s.replace("t", " "))
    if when.tzinfo is None:
        when = when.replace(tzinfo=TZ)
    return when.timestamp()


def text_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()[:16]


def err(e: Exception) -> str:
    if isinstance(e, praw.exceptions.RedditAPIException):
        return "Reddit refused: " + "; ".join(f"{i.error_type}: {i.message}" for i in e.items)
    if isinstance(e, PrawcoreException):
        return f"Reddit API error: {e}. Check REDDIT_* credentials and that the app is a 'script' app."
    return f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- safety ---

class SafetyGuard:
    """Refuses write actions that look like spam to Reddit's filters."""

    @staticmethod
    def check(kind: str, text: str, subreddit: str = "") -> None:
        now = time.time()
        gap = MIN_GAP_POST if kind == "post" else MIN_GAP_COMMENT
        cap = MAX_POSTS_DAY if kind == "post" else MAX_COMMENTS_DAY
        last = q("select max(ts) as t from actions where kind=%s", (kind,), one=True)["t"]
        if last and now - last < gap:
            raise Blocked(f"too soon after the previous {kind}, wait {int(gap - (now - last))} s")
        n = q("select count(*) as n from actions where kind=%s and ts>%s", (kind, now - 86400), one=True)["n"]
        if n >= cap:
            raise Blocked(f"daily limit reached ({cap} {kind}s per 24 h)")
        if kind == "post" and subreddit:
            last_sub = q("select max(ts) as t from actions where kind='post' and lower(subreddit)=lower(%s)",
                         (subreddit,), one=True)["t"]
            if last_sub and now - last_sub < MIN_GAP_SAME_SUB_POST:
                raise Blocked(f"already posted in r/{subreddit} within {MIN_GAP_SAME_SUB_POST // 3600} h")
        if q("select 1 from actions where hash=%s and ts>%s", (text_hash(text), now - 7 * 86400), one=True):
            raise Blocked("the same text was already published in the last 7 days")

    @staticmethod
    def record(kind: str, text: str, thing_id: str, subreddit: str = "") -> None:
        q("insert into actions (kind, ts, hash, thing_id, subreddit) values (%s, %s, %s, %s, %s)",
          (kind, time.time(), text_hash(text), thing_id, subreddit))
        q("delete from actions where ts < %s", (time.time() - 30 * 86400,))


# ---------------------------------------------------------------- reddit ---

_reddit: Optional[praw.Reddit] = None


def reddit() -> praw.Reddit:
    global _reddit
    if _reddit is None:
        missing = [k for k in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME",
                               "REDDIT_PASSWORD", "REDDIT_USER_AGENT") if not os.getenv(k)]
        if missing:
            raise RuntimeError(f"Missing settings: {', '.join(missing)}. See .env.example.")
        _reddit = praw.Reddit(
            client_id=os.environ["REDDIT_CLIENT_ID"],
            client_secret=os.environ["REDDIT_CLIENT_SECRET"],
            username=os.environ["REDDIT_USERNAME"],
            password=os.environ["REDDIT_PASSWORD"],
            user_agent=os.environ["REDDIT_USER_AGENT"],
            ratelimit_seconds=600,  # PRAW waits out Reddit's own rate limits instead of failing
        )
    return _reddit


def fullname(ref: str) -> str:
    """Post/comment URL, t1_/t3_/t4_ fullname or bare post id -> fullname."""
    ref = ref.strip()
    m = re.search(r"/comments/([a-z0-9]+)(?:/[^/]*/([a-z0-9]+))?", ref)
    if m:
        return f"t1_{m.group(2)}" if m.group(2) else f"t3_{m.group(1)}"
    if re.fullmatch(r"t[134]_[a-z0-9]+", ref):
        return ref
    if re.fullmatch(r"[a-z0-9]+", ref):
        return "t3_" + ref
    raise ValueError(f"Not a Reddit post/comment id or URL: {ref}")


def thing(ref: str):
    fn = fullname(ref)
    r, raw = reddit(), fn[3:]
    return {"t1_": lambda: r.comment(raw), "t3_": lambda: r.submission(id=raw),
            "t4_": lambda: r.inbox.message(raw)}[fn[:3]]()


def link(obj) -> str:
    return f"https://reddit.com{obj.permalink}" if getattr(obj, "permalink", None) else "(message)"


def reply_to(parent: str, text: str) -> str:
    SafetyGuard.check("comment", text)
    c = thing(parent).reply(text.strip() + (f"\n\n{REPLY_FOOTER}" if REPLY_FOOTER else ""))
    SafetyGuard.record("comment", text, c.fullname if c else "")
    return link(c)


def submit_post(subreddit: str, title: str, body: str = "", url: str = "", flair_id: str = "") -> str:
    subreddit = subreddit.removeprefix("r/").removeprefix("/r/")
    SafetyGuard.check("post", title + body + url, subreddit)
    sub = reddit().subreddit(subreddit)
    kw = {"flair_id": flair_id} if flair_id else {}
    s = sub.submit(title, url=url, **kw) if url else sub.submit(title, selftext=body, **kw)
    SafetyGuard.record("post", title + body + url, s.fullname, subreddit)
    return link(s)


def edit_thing(ref: str, text: str) -> str:
    obj = thing(ref)
    if str(obj.author).lower() != os.environ["REDDIT_USERNAME"].lower():
        raise ValueError("You can only edit your own posts and comments.")
    obj.edit(body=text)
    return link(obj)


def whoami_text() -> str:
    me = reddit().user.me()
    day = time.time() - 86400
    c = q("select count(*) as n from actions where kind='comment' and ts>%s", (day,), one=True)["n"]
    p = q("select count(*) as n from actions where kind='post' and ts>%s", (day,), one=True)["n"]
    return (f"u/{me.name} · post karma {me.link_karma} · comment karma {me.comment_karma} · "
            f"since {fmt(me.created_utc)}\nLast 24 h: {c}/{MAX_COMMENTS_DAY} comments, {p}/{MAX_POSTS_DAY} posts")


def subreddit_rules_text(subreddit: str) -> str:
    sub = reddit().subreddit(subreddit.removeprefix("r/"))
    rules = [f"{i + 1}. {r.short_name}: {r.description[:400]}" for i, r in enumerate(sub.rules)]
    try:
        req = sub.post_requirements()
    except Exception:
        req = {}
    req = {k: v for k, v in req.items() if v not in (None, [], "", False, 0)}
    return (f"r/{sub.display_name} · {sub.subscribers} subscribers\n{sub.public_description}\n\nRules:\n"
            + ("\n".join(rules) or "(none listed)") + f"\n\nPost requirements: {req or 'none'}")


def fetch_unread(limit: int = 25) -> list[dict]:
    """Unread inbox items with the context needed to write a reply."""
    r = reddit()
    me = r.user.me().name.lower()
    out = []
    for it in r.inbox.unread(limit=limit):
        author = str(it.author) if it.author else "[deleted]"
        d = {"id": it.fullname, "author": author, "body": it.body[:3000], "subreddit": "",
             "link": "", "post_title": "", "post_text": "", "parent_text": "",
             "subject": getattr(it, "subject", ""), "is_self": author.lower() == me}
        if isinstance(it, praw.models.Comment):
            parent = it.parent()
            d.update(subreddit=str(it.subreddit), link=f"https://reddit.com{it.context}",
                     post_title=it.submission.title, post_text=it.submission.selftext[:2000],
                     parent_text=(parent.body if isinstance(parent, praw.models.Comment)
                                  else parent.selftext)[:1500])
        out.append(d)
    return out


def mark_read(refs: list[str]) -> None:
    reddit().inbox.mark_read([thing(x) for x in refs])


# ----------------------------------------------------------------- queue ---

def schedule_post(subreddit: str, title: str, body: str, url: str, flair_id: str, publish_at: float) -> str:
    sid = uuid.uuid4().hex[:6]
    q("insert into scheduled (id, subreddit, title, body, url, flair_id, publish_at, status) "
      "values (%s, %s, %s, %s, %s, %s, %s, 'pending')",
      (sid, subreddit.removeprefix("r/"), title, body, url, flair_id, publish_at))
    return sid


def list_scheduled(include_done: bool = False) -> list[dict]:
    where = "" if include_done else "where status='pending'"
    return q(f"select * from scheduled {where} order by publish_at")


def cancel_scheduled(sid: str) -> bool:
    return bool(q("update scheduled set status='cancelled' where id=%s and status='pending' returning id",
                  (sid,), one=True))


def run_due_posts() -> list[tuple[str, str]]:
    """Publish the oldest due post (one per run so the gap between posts holds).

    Returns (status, message) pairs; status is published / failed / postponed / idle.
    """
    due = q("select * from scheduled where status='pending' and publish_at<=%s order by publish_at",
            (time.time(),))
    if not due:
        return [("idle", "Nothing due.")]
    item = due[0]
    try:
        url = submit_post(item["subreddit"], item["title"], item["body"], item["url"], item["flair_id"])
    except Blocked as b:
        return [("postponed", f"{item['id']} postponed: {b}")]
    except Exception as e:
        q("update scheduled set status='failed', error=%s where id=%s", (err(e), item["id"]))
        return [("failed", f"{item['id']} «{item['title']}» failed: {err(e)}")]
    q("update scheduled set status='published', link=%s where id=%s", (url, item["id"]))
    return [("published", f"«{item['title']}» published in r/{item['subreddit']}: {url}")]
