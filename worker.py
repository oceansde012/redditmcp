"""Telegram reminder bot (Render, free). Posts with images are prepared in advance and stored
in Postgres; at publish time the bot sends the images plus copy-ready title and text, and you
publish on Reddit yourself. No Reddit API, no paid services. Pings itself so it never sleeps.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
import uuid
from datetime import timedelta

from aiohttp import ClientSession, ClientTimeout, FormData, web

import core

log = logging.getLogger("bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER = int(os.getenv("TELEGRAM_OWNER_ID") or 0)
DIGEST_HOUR = core.env_int("DIGEST_HOUR", 10)

HELP = """Я нагадую, коли публікувати пост, і даю все готове: картинки, заголовок і текст (тапни, щоб скопіювати).

/today — що сьогодні
/queue — уся черга
/cancel <id> — прибрати пост
/post r/сабредіт 18:30   (або 27.10 18:30)
Заголовок
Текст — додати свій пост вручну"""

state = {"await_link": None, "last_error": ""}


class Telegram:
    def __init__(self, session: ClientSession):
        self.s = session
        self.base = f"https://api.telegram.org/bot{TOKEN}/"

    async def call(self, method: str, form: FormData | None = None, **params):
        kw = {"data": form} if form else {"json": params}
        async with self.s.post(self.base + method, timeout=ClientTimeout(total=120), **kw) as r:
            data = await r.json()
        if not data.get("ok"):
            log.warning("telegram %s: %s", method, data.get("description"))
        return data.get("result")

    async def send(self, text: str, buttons: list | None = None, chat_id: int | None = None, html_mode=False):
        params = {"chat_id": chat_id or OWNER, "text": text[:4096], "disable_web_page_preview": True}
        if html_mode:
            params["parse_mode"] = "HTML"
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return await self.call("sendMessage", **params)

    async def send_images(self, images: list[dict]) -> None:
        if not images:
            return
        form = FormData()
        form.add_field("chat_id", str(OWNER))
        if len(images) == 1:
            form.add_field("photo", bytes(images[0]["data"]), filename=images[0]["filename"])
            await self.call("sendPhoto", form)
            return
        group = []
        for i, img in enumerate(images[:10]):
            form.add_field(f"f{i}", bytes(img["data"]), filename=img["filename"])
            group.append({"type": "photo", "media": f"attach://f{i}"})
        form.add_field("media", json.dumps(group))
        await self.call("sendMediaGroup", form)


def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def post_card(p: dict, header: str) -> str:
    e = html.escape
    parts = [f"{header} · <b>r/{e(p['subreddit'])}</b> · <code>{p['id']}</code>",
             "", "<b>Заголовок</b> (тапни, щоб скопіювати):", f"<code>{e(p['title'])}</code>"]
    if p["body"]:
        parts += ["", "<b>Текст:</b>", f"<pre>{e(p['body'][:3000])}</pre>"]
    if p["notes"]:
        parts += ["", f"💡 {e(p['notes'])}"]
    return "\n".join(parts)


def post_buttons(p: dict) -> list:
    return [[{"text": f"↗️ Відкрити r/{p['subreddit']}", "url": f"https://www.reddit.com/r/{p['subreddit']}/submit"}],
            [btn("✅ Опублікував", f"done:{p['id']}"), btn("⏰ +1 год", f"snooze:{p['id']}")],
            [btn("📅 Завтра", f"tomorrow:{p['id']}"), btn("❌ Пропустити", f"skip:{p['id']}")]]


async def remind(tg: Telegram, p: dict) -> None:
    images = core.q("select filename, data from media where post_id=%s order by ord", (p["id"],))
    await tg.send_images(images)
    await tg.send(post_card(p, "🔔 Час публікувати"), post_buttons(p), html_mode=True)
    core.q("update posts set status='reminded' where id=%s", (p["id"],))


# ------------------------------------------------------------- handlers ---

async def on_callback(tg: Telegram, cb: dict) -> None:
    action, _, pid = cb.get("data", "").partition(":")
    p = core.q("select * from posts where id=%s", (pid,), one=True)
    msg = cb["message"]

    async def answer(text: str):
        await tg.call("answerCallbackQuery", callback_query_id=cb["id"], text=text)

    async def drop_buttons():
        await tg.call("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                      reply_markup={"inline_keyboard": []})

    if not p or p["status"] in ("done", "skipped"):
        return await answer("Вже оброблено")
    if action == "done":
        core.q("update posts set status='done' where id=%s", (pid,))
        state["await_link"] = pid
        await answer("🔥")
        await drop_buttons()
        await tg.send("Супер! Скинь посилання на пост: я дивитимусь, що заходить, і підлаштовуватиму наступні.")
    elif action in ("snooze", "tomorrow"):
        delay = 3600 if action == "snooze" else 86400
        core.q("update posts set status='pending', publish_at=%s where id=%s", (time.time() + delay, pid))
        await answer("Нагадаю " + ("через годину" if action == "snooze" else "завтра"))
        await drop_buttons()
    elif action == "skip":
        core.q("update posts set status='skipped' where id=%s", (pid,))
        await answer("Пропущено")
        await drop_buttons()


def queue_text(rows: list[dict]) -> str:
    icons = {"pending": "🕓", "reminded": "🔔"}
    return "\n".join(f"{icons.get(r['status'], '')} {core.fmt(r['publish_at'])} · r/{r['subreddit']} · "
                     f"{r['title'][:60]} · {r['id']}" for r in rows)


async def on_message(tg: Telegram, text: str) -> None:
    first, _, rest = text.strip().partition("\n")
    cmd, _, args = first.partition(" ")
    cmd = cmd.split("@")[0].lower() if cmd.startswith("/") else ""
    rest, args = rest.strip(), args.strip()

    if state["await_link"] and not cmd and "reddit.com" in text:
        core.q("update posts set link=%s where id=%s", (text.strip(), state["await_link"]))
        state["await_link"] = None
        return await tg.send("Записав 👍")

    if cmd == "/today":
        start = core.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = core.q("select * from posts where status in ('pending','reminded') and publish_at<%s "
                      "order by publish_at", ((start + timedelta(days=1)).timestamp(),))
        await tg.send(queue_text(rows) or "Сьогодні нічого. Відпочивай 😌")
    elif cmd == "/queue":
        rows = core.q("select * from posts where status in ('pending','reminded') order by publish_at")
        await tg.send(queue_text(rows) or "Черга порожня.")
    elif cmd == "/cancel":
        ok = core.q("update posts set status='skipped' where id=%s and status in ('pending','reminded') "
                    "returning id", (args,), one=True)
        await tg.send("Прибрав." if ok else "Не знайшов такого поста.")
    elif cmd == "/post":
        sub, _, when = args.partition(" ")
        title, _, body = rest.partition("\n")
        if not sub or not title:
            return await tg.send("Формат:\n/post r/сабредіт 18:30\nЗаголовок\nТекст")
        sub = sub.removeprefix("/").removeprefix("r/")
        ts = core.parse_when(when)
        pid = uuid.uuid4().hex[:6]
        core.q("insert into posts (id, subreddit, title, body, notes, publish_at, created) "
               "values (%s, %s, %s, %s, '', %s, %s)", (pid, sub, title.strip(), body.strip(), ts, time.time()))
        warn = core.spacing_warnings(sub, ts, pid)
        await tg.send(f"🗓 Нагадаю {core.fmt(ts)} · r/{sub} · {pid}" + ("".join(f"\n⚠️ {w}" for w in warn)))
    else:
        await tg.send(HELP)


async def handle_update(tg: Telegram, u: dict) -> None:
    chat = (u.get("message") or u.get("callback_query", {}).get("message") or {}).get("chat", {})
    if not OWNER:
        if chat.get("id"):
            await tg.send(f"Твій chat id: {chat['id']}\nВпиши його в TELEGRAM_OWNER_ID на Render.", chat_id=chat["id"])
        return
    if chat.get("id") != OWNER:
        return  # only the owner can use the bot
    if "callback_query" in u:
        await on_callback(tg, u["callback_query"])
    elif u.get("message", {}).get("text"):
        await on_message(tg, u["message"]["text"])


# ---------------------------------------------------------------- loops ---

async def digest(tg: Telegram) -> None:
    today = core.now()
    if today.hour < DIGEST_HOUR or core.kv_get("digest_day") == today.strftime("%Y-%m-%d"):
        return
    core.kv_set("digest_day", today.strftime("%Y-%m-%d"))
    end = today.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    rows = core.q("select * from posts where status in ('pending','reminded') and publish_at<%s "
                  "order by publish_at", (end.timestamp(),))
    if rows:
        await tg.send("☀️ Сьогодні в планах:\n" + queue_text(rows) + "\n\nНагадаю кожен у свій час.")


async def reminder_loop(tg: Telegram) -> None:
    while True:
        try:
            for p in core.q("select * from posts where status='pending' and publish_at<=%s order by publish_at",
                            (time.time(),)):
                await remind(tg, p)
            await digest(tg)
            state["last_error"] = ""
        except Exception as e:
            log.exception("reminder loop")
            if str(e) != state["last_error"]:
                state["last_error"] = str(e)
                await tg.send(f"⚠️ {type(e).__name__}: {e}")
        await asyncio.sleep(60)


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
                await tg.send(f"⚠️ {type(e).__name__}: {e}")


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
        loops = [telegram_loop(tg), keepalive(session)]
        if OWNER:
            await tg.send("🟢 Бот на зв'язку. /today — що сьогодні, /queue — уся черга")
            loops.append(reminder_loop(tg))
        await asyncio.gather(*loops)


if __name__ == "__main__":
    asyncio.run(main())
