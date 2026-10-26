# redditmcp

Безкоштовний помічник для Reddit:

- **Claude** щотижня сам готує пости про [Dotline Widgets](https://github.com/oceansde012/dotline-widgets): тексти, картинки, сабредіти й час. Правила описані в [CONTENT_PLAN.md](CONTENT_PLAN.md).
- **Telegram-бот** на безкоштовному Render нагадує в потрібний час. Він надсилає картинки, заголовок і текст (копіюються одним тапом) і кнопку переходу в сабредіт.
- **Публікуєш ти сам.** Reddit API не використовується, тож бану за бота не буде.

```
Claude (щотижня, на ПК) ──► Neon Postgres (free) ◄── worker.py на Render (free) ──► Telegram
```

## Бот

| Команда | Що робить |
|---|---|
| `/today` | що сьогодні |
| `/queue` | уся черга |
| `/cancel <id>` | прибрати пост |
| `/post r/сабредіт 18:30` + заголовок + текст | додати свій пост |

Кнопки в нагадуванні: ✅ Опублікував (потім скинь посилання), ⏰ +1 год, 📅 Завтра, ❌ Пропустити. О 10:00 бот надсилає план на день.

## Налаштування Render
New → Blueprint → цей репо. Впиши `DATABASE_URL` (Neon) і `TELEGRAM_BOT_TOKEN` (@BotFather). Напиши боту, він відповість chat id. Впиши його в `TELEGRAM_OWNER_ID`.

Сервіс сам себе пінгує кожні 10 хв, тож безкоштовний Render не засинає. Цілий місяць вкладається у 750 год, якщо інші сервіси у воркспейсі вимкнені.

## Контент вручну
```
python content/posts.py list                       # черга
python content/posts.py add content/week1/plan.json
python content/posts.py done                       # що опубліковано
```
