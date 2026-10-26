"""Manage the post queue the Telegram bot reminds about.

  python content/posts.py add plan.json   add posts: [{subreddit, when, title, body, notes, images: [paths]}]
  python content/posts.py list            upcoming posts
  python content/posts.py done            published/skipped posts with links
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core  # noqa: E402


def add(plan_path: str) -> None:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    base = Path(plan_path).parent
    for p in plan:
        pid = uuid.uuid4().hex[:6]
        ts = core.parse_when(p["when"])
        sub = p["subreddit"].removeprefix("r/")
        warn = core.spacing_warnings(sub, ts)
        core.q("insert into posts (id, subreddit, title, body, notes, publish_at, created) "
               "values (%s, %s, %s, %s, %s, %s, %s)",
               (pid, sub, p["title"], p.get("body", ""), p.get("notes", ""), ts, time.time()))
        for i, img in enumerate(p.get("images", [])):
            path = Path(img) if Path(img).is_absolute() else base / img
            core.q("insert into media (post_id, ord, filename, data) values (%s, %s, %s, %s)",
                   (pid, i, path.name, path.read_bytes()))
        print(f"{pid}  {core.fmt(ts)}  r/{sub}  {p['title'][:60]}" + "".join(f"\n   ! {w}" for w in warn))


def list_posts() -> None:
    for r in core.q("select p.*, (select count(*) from media m where m.post_id=p.id) as imgs from posts p "
                    "where status in ('pending','reminded') order by publish_at"):
        print(f"{r['id']}  {core.fmt(r['publish_at'])}  {r['status']:<8} r/{r['subreddit']:<18} "
              f"{r['imgs']} img  {r['title'][:60]}")


def done() -> None:
    """Published posts with the links you sent to the bot (open them to see how they did)."""
    for r in core.q("select * from posts where status in ('done','skipped') order by publish_at desc limit 30"):
        print(f"{core.fmt(r['publish_at'])}  {r['status']:<7} r/{r['subreddit']:<18} {r['title'][:50]}  {r['link'] or ''}")


if __name__ == "__main__":
    core.init_db()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    {"add": lambda: add(sys.argv[2]), "list": list_posts, "done": done}[cmd]()
