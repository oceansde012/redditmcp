# Content plan (read by Claude every week before preparing posts)

## Goal
Grow awareness of **Dotline Widgets** (https://github.com/oceansde012/dotline-widgets) on Reddit
without getting the account or posts removed for spam. The owner publishes by hand; the Telegram
bot only reminds.

## Facts
Use only facts from the Dotline README (fetch it fresh each week, it changes) and from what the owner
said. Never invent features, numbers, download counts, a personal story or a Play Store listing.

## Cadence and spacing
- 3–4 posts per week, max 1 per day, at 17:00–18:30 Kyiv (US morning).
- Same subreddit: at most once every 3 weeks. Never repost the same title or body; rewrite from scratch.
- `posts.py add` prints warnings for spacing; fix the plan if it does.

## Subreddits (rotate; skip any where a past post was removed)
| Subreddit | Angle |
|---|---|
| r/fossdroid, r/opensource, r/coolgithubprojects | open source, privacy, MIT |
| r/NothingTech | Nothing-style look on any phone, say it is not official |
| r/androidthemes | setup screenshot of the owner's home screen, flair Setup, components in first comment |
| r/androidapps | [DEV] tag, ask for feedback |
| r/SideProject, r/indiehackers | build story from facts: stack, CI releases, self-update |
| r/oneui, r/samsung | tested on One UI, "Allow restricted settings" tip |
| r/androiddev, r/Kotlin | only real technical questions or lessons the owner confirms; no ads |
| r/Android | only in their weekly self-promo/app thread, as a comment |

## Value over promo
Roughly every third item should be useful on its own: a how-to (e.g. making a minimal dot-matrix
home screen), a comparison of widget approaches, a question to the community. Mention Dotline only
where relevant.

## Images
`content/images.py`: `showcase()` for widget screenshots, `text_card()` for lists. 1440×1080,
dark, one red dot. Look at every generated image before queueing it. Vary images week to week.

## Weekly routine
1. `python content/posts.py done` to see what was published (links show how it went) and what was skipped.
2. Fetch the README for new widgets or changes.
3. Write `content/<YYYY-MM-DD>/plan.json` and its images, then `python content/posts.py add` with that file.
4. Commit and push.
