"""Cloud worker (Render): watches the Reddit inbox, drafts replies with Claude, asks for
one-tap approval in Telegram, publishes scheduled posts. Also serves /health and pings
itself so a free Render instance never sleeps.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid

import anthropic
from aiohttp import ClientSession, ClientTimeout, web

import core

log = logging.getLogger("worker")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER = int(os.getenv("TELEGRAM_OWNER_ID") or 0)
POLL_SECONDS = core.env_int("POLL_SECONDS", 300)
MAX_DRAFTS_PER_TICK = core.env_int("MAX_DRAFTS_PER_TICK", 5)
IGNORE = {u.strip().lower() for u in (os.getenv("IGNORE_USERS") or "AutoModerator").split(",") if u.strip()}
MODEL = os.getenv("CLAUDE_MODEL") or "claude-sonnet-5-5"
PERSONA = os.getenv("PERSONA") or "(not provided)"

HELP = """Команди:
/post r/сабредіт 2026-10-27 18:30   (або 18:30, або зараз)
Заголовок
Текст поста (або одне посилання для link-поста)

/edit <посилання або id>
Новий текст поста чи коментаря

/queue — черга постів
/cancel <id> — скасувати пост
/rules r/сабредіт — правила сабредіту
/stats — акаунт і ліміти
/check — перевірити вхідні зараз
/pause, /resume — зупинити або відновити автопідготовку відповідей"""

SYSTEM = """You write Reddit replies on behalf of the account owner.
About the owner: {persona}

Rules:
- Reply in the same language as the comment you are answering.
- Sound like a regular member of this subreddit: short, specific, helpful. No corporate tone,
  no hashtags, no emojis unless they used them.
- Answer what was actually said. Don't push links or products unless directly asked.
- Never invent facts about the owner or their product; if unsure, stay general or ask back.
- If no reply is needed (plain thanks, spam, trolling, nothing to add), output exactly SKIP.
Output only the reply text."""

state = {"edit": None, "last_error": "", "claude": None}
tick_lock = asyncio.Lock()


def draft_reply(item: dict, avoid: str = "") -> str:
    ctx = "\n".join(f"{k}: {v}" for k, v in [
        ("Subreddit", f"r/{item['subreddit']}" if item["subreddit"] else "private message"),
        ("Message subject", item.get("subject")), ("Post title", item["post_title"]),
        ("Post text", item["post_text"]), ("The owner's text they replied to", item["parent_text"]),
        (f"Comment from u/{item['author']} (reply to this)", item["body"])] if v)
    if avoid:
        ctx += f"\n\nWrite a noticeably different reply than this one:\n{avoid}"
    state["claude"] = state["claude"] or anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    msg = state["claude"].messages.create(model=MODEL, max_tokens=700, system=SYSTEM.format(persona=PERSONA),
                                 messages=[{"role": "user", "content": ctx}])
    return "".join(b.text for b in msg.content if b.type == "text").strip()


# -------------------------------------------------------------- telegram ---

class Telegram:
    def __init__(self, session: ClientSession):
        self.s = session
        self.base = f"https://api.telegram.org/bot{TOKEN}/"

    async def call(self, method: str, **params):
        async with self.s.post(self.base + method, json=params, timeout=ClientTimeout(total=70)) as r:
            data = await r.json()
        if not data.get("ok"):
            log.warning("telegram %s: %s", method, data.get("description"))
        return data.get("result")

    async def send(self, text: str, buttons: list | None = None, chat_id: int | None = None):
        params = {"chat_id": chat_id or OWNER, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return await self.call("sendMessage", **params)


def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def buttons_for(d: dict) -> list:
    if d["kind"] == "reply":
        return [[btn("✅ Відправити", f"send:{d['id']}"), btn("✏️ Змінити", f"edit:{d['id']}")],
                [btn("🔄 Інший варіант", f"regen:{d['id']}"), btn("❌ Пропустити", f"skip:{d['id']}")]]
    return [[btn("✅ Підтвердити", f"send:{d['id']}"), btn("❌ Скасувати", f"skip:{d['id']}")]]


def card(d: dict) -> str:
    c = json.loads(d["context"])
    if d["kind"] == "reply":
        head = f"💬 u/{c['author']} " + (f"у r/{c['subreddit']}" if c["subreddit"] else "(особисте)")
        if c["post_title"]:
            head += f"\n📌 {c['post_title']}"
        head += f"\n\n{c['body'][:1200]}" + (f"\n🔗 {c['link']}" if c["link"] else "")
        body = "🤐 Claude радить не відповідати." if d["body"] == "SKIP" else f"✍️ Чернетка:\n{d['body']}"
        return f"{head}\n\n{body}"
    if d["kind"] == "edit":
        return f"✏️ Оновити {d['target']}:\n\n{d['body']}"
    return f"📝 Пост у r/{c['subreddit']} зараз:\n{c['title']}\n\n{c.get('url') or d['body']}"


def new_draft(kind: str, target: str, body: str, context: dict) -> dict:
    d = {"id": uuid.uuid4().hex[:10], "kind": kind, "target": target, "body": body,
         "context": json.dumps(context, ensure_ascii=False)}
    core.q("insert into drafts (id, kind, target, context, body, status, created) "
           "values (%s, %s, %s, %s, %s, 'pending', %s)",
           (d["id"], kind, target, d["context"], body, time.time()))
    return d


async def show(tg: Telegram, d: dict) -> None:
    m = await tg.send(card(d), buttons_for(d))
    if m:
        core.q("update drafts set tg_message_id=%s where id=%s", (m["message_id"], d["id"]))


def execute(d: dict) -> str:
    if d["kind"] == "reply":
        return core.reply_to(d["target"], d["body"])
    if d["kind"] == "edit":
        return core.edit_thing(d["target"], d["body"])
    c = json.loads(d["context"])
    return core.submit_post(c["subreddit"], c["title"], d["body"], c.get("url", ""))


async def process_approved(tg: Telegram, announce_id: str = "") -> None:
    """Send approved drafts; ones blocked by the limits stay approved and go on a later tick."""
    for d in core.q("select * from drafts where status='approved' order by created"):
        try:
            url = await asyncio.to_thread(execute, d)
        except core.Blocked as b:
            if d["id"] == announce_id:
                await tg.send(f"⏳ Поставив у чергу ({b}). Відправлю автоматично.")
            continue
        except Exception as e:
            core.q("update drafts set status='failed', result=%s where id=%s", (core.err(e), d["id"]))
            await tg.send(f"⚠️ Не вдалося: {core.err(e)}")
            continue
        core.q("update drafts set status='done', result=%s where id=%s", (url, d["id"]))
        await tg.send(f"✅ Відправлено: {url}")


# ------------------------------------------------------------- handlers ---

async def on_callback(tg: Telegram, cb: dict) -> None:
    action, _, did = cb.get("data", "").partition(":")
    d = core.q("select * from drafts where id=%s", (did,), one=True)
    msg = cb["message"]

    async def answer(text: str):
        await tg.call("answerCallbackQuery", callback_query_id=cb["id"], text=text)

    async def drop_buttons():
        await tg.call("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                      reply_markup={"inline_keyboard": []})

    if not d or d["status"] != "pending":
        return await answer("Вже оброблено")
    if action == "send":
        if d["body"] == "SKIP":
            return await answer("Спершу 🔄 або ✏️")
        core.q("update drafts set status='approved' where id=%s", (did,))
        await answer("Відправляю…")
        await drop_buttons()
        await process_approved(tg, announce_id=did)
    elif action == "skip":
        core.q("update drafts set status='skipped' where id=%s", (did,))
        await answer("Пропущено")
        await drop_buttons()
    elif action == "edit":
        state["edit"] = did
        await answer("Чекаю текст")
        await tg.send("Надішли свій текст відповіді одним повідомленням. /cancel_edit — скасувати.")
    elif action == "regen":
        await answer("Пишу інший варіант…")
        d["body"] = await asyncio.to_thread(draft_reply, json.loads(d["context"]), d["body"])
        core.q("update drafts set body=%s where id=%s", (d["body"], did))
        await tg.call("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                      text=card(d)[:4000], disable_web_page_preview=True,
                      reply_markup={"inline_keyboard": buttons_for(d)})


async def on_message(tg: Telegram, text: str) -> None:
    first, _, rest = text.strip().partition("\n")
    cmd, _, args = first.partition(" ")
    cmd = cmd.split("@")[0].lower() if cmd.startswith("/") else ""
    rest, args = rest.strip(), args.strip()

    if state["edit"] and not cmd:
        did, state["edit"] = state["edit"], None
        core.q("update drafts set body=%s, status='approved' where id=%s and status='pending'", (text.strip(), did))
        return await process_approved(tg, announce_id=did)

    if cmd == "/cancel_edit":
        state["edit"] = None
        await tg.send("Ок, редагування скасовано.")
    elif cmd == "/post":
        sub, _, when = args.partition(" ")
        title, _, body = rest.partition("\n")
        body = body.strip()
        if not sub or not title:
            return await tg.send("Формат:\n/post r/сабредіт 18:30\nЗаголовок\nТекст")
        url = body if body.startswith("http") and "\n" not in body else ""
        sub = sub.removeprefix("r/").removeprefix("/r/")
        ts = core.parse_when(when)
        if ts is None:
            ctx = {"subreddit": sub, "title": title.strip(), "url": url}
            await show(tg, new_draft("post", sub, "" if url else body, ctx))
        else:
            sid = core.schedule_post(sub, title.strip(), "" if url else body, url, "", ts)
            await tg.send(f"🗓 Заплановано {sid} на {core.fmt(ts)} у r/{sub}.\n"
                          f"Порада: перевір /rules r/{sub}")
    elif cmd == "/edit":
        if not args or not rest:
            return await tg.send("Формат:\n/edit <посилання>\nНовий текст")
        await show(tg, new_draft("edit", core.fullname(args), rest, {}))
    elif cmd == "/queue":
        rows = core.list_scheduled()
        await tg.send("\n".join(f"{r['id']} · {core.fmt(r['publish_at'])} · r/{r['subreddit']} · {r['title']}"
                                for r in rows) or "Черга порожня.")
    elif cmd == "/cancel":
        await tg.send("Скасовано." if core.cancel_scheduled(args) else "Не знайшов такого поста в черзі.")
    elif cmd == "/rules":
        await tg.send(await asyncio.to_thread(core.subreddit_rules_text, args or "test"))
    elif cmd == "/stats":
        pending = core.q("select count(*) as n from drafts where status in ('pending','approved')", one=True)["n"]
        paused = " · ⏸ на паузі" if core.kv_get("paused") == "1" else ""
        await tg.send(await asyncio.to_thread(core.whoami_text) + f"\nЧернеток чекає: {pending}{paused}")
    elif cmd == "/check":
        await tg.send("Перевіряю…")
        await tick(tg)
    elif cmd in ("/pause", "/resume"):
        core.kv_set("paused", "1" if cmd == "/pause" else "0")
        await tg.send("⏸ Пауза: нових чернеток не буде, пости за розкладом ідуть." if cmd == "/pause"
                      else "▶️ Продовжую.")
    else:
        await tg.send(HELP)


async def handle_update(tg: Telegram, u: dict) -> None:
    chat = (u.get("message") or u.get("callback_query", {}).get("message") or {}).get("chat", {})
    if not OWNER:
        if chat.get("id"):
            await tg.send(f"Твій chat id: {chat['id']}\nДодай його в TELEGRAM_OWNER_ID на Render.", chat_id=chat["id"])
        return
    if chat.get("id") != OWNER:
        return  # only the owner can control the bot
    if "callback_query" in u:
        await on_callback(tg, u["callback_query"])
    elif u.get("message", {}).get("text"):
        await on_message(tg, u["message"]["text"])


# ---------------------------------------------------------------- loops ---

async def check_inbox(tg: Telegram) -> None:
    items = await asyncio.to_thread(core.fetch_unread)
    new = [i for i in items if not core.q("select 1 from seen where id=%s", (i["id"],), one=True)]
    for it in new[:MAX_DRAFTS_PER_TICK]:
        core.q("insert into seen (id, ts) values (%s, %s) on conflict do nothing", (it["id"], time.time()))
        if it["is_self"] or it["author"].lower() in IGNORE:
            continue
        body = await asyncio.to_thread(draft_reply, it)
        await show(tg, new_draft("reply", it["id"], body, it))
    if new:
        await asyncio.to_thread(core.mark_read, [i["id"] for i in new[:MAX_DRAFTS_PER_TICK]])


async def tick(tg: Telegram) -> None:
    async with tick_lock:  # /check and the timer must not draft the same comment twice
        await _tick(tg)


async def _tick(tg: Telegram) -> None:
    try:
        if core.kv_get("paused") != "1":
            await check_inbox(tg)
        for status, text in await asyncio.to_thread(core.run_due_posts):
            if status in ("published", "failed"):
                await tg.send(("🚀 " if status == "published" else "⚠️ ") + text)
        await process_approved(tg)
        state["last_error"] = ""
    except Exception as e:
        log.exception("tick failed")
        if core.err(e) != state["last_error"]:  # don't repeat the same error every few minutes
            state["last_error"] = core.err(e)
            await tg.send(f"⚠️ Помилка: {core.err(e)}")


async def reddit_loop(tg: Telegram) -> None:
    while True:
        await tick(tg)
        await asyncio.sleep(POLL_SECONDS)


async def telegram_loop(tg: Telegram) -> None:
    offset = int(core.kv_get("tg_offset") or 0)
    while True:
        try:
            updates = await tg.call("getUpdates", offset=offset, timeout=50,
                                    allowed_updates=["message", "callback_query"])
        except Exception:
            log.exception("getUpdates failed")
            updates = None
        if updates is None:
            await asyncio.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            core.kv_set("tg_offset", str(offset))
            try:
                await handle_update(tg, u)
            except Exception as e:
                log.exception("update failed")
                await tg.send(f"⚠️ {core.err(e)}")


async def keepalive(session: ClientSession) -> None:
    """Free Render services sleep after 15 min without requests; ping ourselves every 10."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    while url:
        await asyncio.sleep(600)
        try:
            async with session.get(url + "/health", timeout=ClientTimeout(total=30)) as r:
                await r.read()
        except Exception as e:
            log.warning("keepalive failed: %s", e)


async def health(_request) -> web.Response:
    return web.Response(text="ok")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    core.init_db()
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT") or 10000)).start()
    async with ClientSession() as session:
        tg = Telegram(session)
        if OWNER:
            await tg.send("🟢 Reddit-бот запущено. /help — команди")
            await asyncio.gather(telegram_loop(tg), reddit_loop(tg), keepalive(session))
        else:  # first run: only tell the user their chat id
            await asyncio.gather(telegram_loop(tg), keepalive(session))


if __name__ == "__main__":
    asyncio.run(main())
