"""Reddit MCP server for Claude Desktop / Claude Code.

Shares the database with the cloud worker, so limits and the post queue are the same
everywhere. Write tools return a preview unless confirm=True.
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

import core

mcp = FastMCP("reddit_mcp")
READ = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}


def safe(fn, *args) -> str:
    try:
        return fn(*args)
    except core.Blocked as b:
        return f"Blocked by safety limits: {b}. Try later."
    except Exception as e:
        return core.err(e)


@mcp.tool(annotations=READ)
def reddit_whoami() -> str:
    """Show the account, karma and how many comments/posts were made in the last 24 h."""
    return safe(core.whoami_text)


@mcp.tool(annotations=READ)
def reddit_get_inbox(limit: int = 25) -> str:
    """List unread inbox items (replies, mentions, messages) with post context, as JSON."""
    return safe(lambda: json.dumps(core.fetch_unread(min(max(limit, 1), 100)), ensure_ascii=False, indent=2))


@mcp.tool(annotations={**WRITE, "idempotentHint": True})
def reddit_mark_read(ids: list[str]) -> str:
    """Mark inbox items as read. ids: fullnames from reddit_get_inbox (t1_..., t4_...)."""
    return safe(lambda: (core.mark_read(ids), f"Marked {len(ids)} read.")[1])


@mcp.tool(annotations=READ)
def reddit_get_subreddit_rules(subreddit: str) -> str:
    """Get a subreddit's rules and post requirements. Read these BEFORE posting there."""
    return safe(core.subreddit_rules_text, subreddit)


@mcp.tool(annotations=READ)
def reddit_list_my_posts(limit: int = 20) -> str:
    """List your recent posts with score, comment count and id."""
    def run():
        subs = core.reddit().user.me().submissions.new(limit=min(max(limit, 1), 100))
        return json.dumps([{"id": s.fullname, "subreddit": str(s.subreddit), "title": s.title,
                            "score": s.score, "comments": s.num_comments, "created": core.fmt(s.created_utc),
                            "url": core.link(s), "removed": bool(getattr(s, "removed_by_category", None))}
                           for s in subs], ensure_ascii=False, indent=2)
    return safe(run)


@mcp.tool(annotations=READ)
def reddit_get_post(post: str, comment_limit: int = 30) -> str:
    """Get a post (id, t3_ id or URL) with its top-level comments."""
    def run():
        s = core.thing(post)
        s.comments.replace_more(limit=0)
        return json.dumps({"id": s.fullname, "subreddit": str(s.subreddit), "title": s.title,
                           "author": str(s.author), "score": s.score, "selftext": s.selftext[:4000],
                           "comments": [{"id": c.fullname, "author": str(c.author), "score": c.score,
                                         "body": c.body[:800]} for c in s.comments[:comment_limit]]},
                          ensure_ascii=False, indent=2)
    return safe(run)


@mcp.tool(annotations=WRITE)
def reddit_reply(parent: str, text: str, confirm: bool = False) -> str:
    """Reply to a comment, post or message (fullname or URL). Preview unless confirm=True.

    Write each reply for its context; identical texts are blocked.
    """
    if not confirm:
        return f"PREVIEW (not sent) → {parent}:\n\n{text}\n\nCall again with confirm=true to send."
    return safe(lambda: "Sent: " + core.reply_to(parent, text))


@mcp.tool(annotations=WRITE)
def reddit_submit_post(subreddit: str, title: str, text: str = "", url: str = "",
                       flair_id: str = "", confirm: bool = False) -> str:
    """Publish a text post (text) or link post (url) now. Check subreddit rules first."""
    if not confirm:
        return f"PREVIEW → r/{subreddit}\n{title}\n\n{url or text}\n\nCall again with confirm=true to publish."
    return safe(lambda: "Published: " + core.submit_post(subreddit, title, text, url, flair_id))


@mcp.tool(annotations={**WRITE, "destructiveHint": True})
def reddit_edit(thing: str, new_text: str, confirm: bool = False) -> str:
    """Replace the body of your own text post or comment (id or URL). Titles can't be edited."""
    if not confirm:
        return f"PREVIEW → new text for {thing}:\n\n{new_text}\n\nCall again with confirm=true."
    return safe(lambda: "Updated: " + core.edit_thing(thing, new_text))


@mcp.tool(annotations={**WRITE, "openWorldHint": False})
def reddit_schedule_post(subreddit: str, title: str, publish_at: str, text: str = "",
                         url: str = "", flair_id: str = "") -> str:
    """Queue a post; the cloud worker publishes it. publish_at: '2026-10-27 18:30' or '18:30'
    (TIMEZONE from settings, default Europe/Kyiv)."""
    def run():
        ts = core.parse_when(publish_at)
        if ts is None:
            return "Use reddit_submit_post to publish now."
        sid = core.schedule_post(subreddit, title, text, url, flair_id, ts)
        return f"Scheduled {sid} for {core.fmt(ts)} in r/{subreddit.removeprefix('r/')}."
    return safe(run)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
def reddit_list_scheduled(include_done: bool = False) -> str:
    """List queued posts and their status."""
    def run():
        rows = [dict(r, publish_at=core.fmt(r["publish_at"])) for r in core.list_scheduled(include_done)]
        return json.dumps(rows, ensure_ascii=False, indent=2) if rows else "Queue is empty."
    return safe(run)


@mcp.tool(annotations={**WRITE, "openWorldHint": False})
def reddit_cancel_scheduled(schedule_id: str) -> str:
    """Cancel a queued post by id."""
    return safe(lambda: f"Cancelled {schedule_id}." if core.cancel_scheduled(schedule_id)
                else f"No pending post {schedule_id}.")


if __name__ == "__main__":
    core.init_db()
    mcp.run()
