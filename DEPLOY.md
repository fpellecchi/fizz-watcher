# Running the Fizz watcher in the cloud

The watcher works unchanged on your PC. These options run it 24/7 instead,
so it survives your laptop sleeping, rebooting, or crashing.

`FIZZ_HEADLESS=1` tells it there is no desktop: no beeping, popup, or
auto-opened browser. Telegram (spams until you reply) and ntfy carry the
alert.

---

## Option A: GitHub Actions (free)

Free unlimited runner minutes **only on public repos** — a private repo
gets 2,000 min/month (~33 h), not enough for 24/7. Your bot token lives in
GitHub Secrets, never in the code, so a public repo is safe here.

Caveat: GitHub's terms describe Actions as CI for the repo's own software,
not always-on compute. Small monitors like this are widely run anyway, but
it is a gray area — Option B is the "correct" way.

`.github/workflows/watch.yml` runs 50-minute stints; the 5-minute cron
keeps one successor queued so handover takes ~30 s. A crashed job is
replaced within 5 minutes.

```bash
cd C:\Users\pelle\Desktop\FizzWatcher
git init
git add .
git commit -m "Fizz Utrecht availability watcher"
gh repo create fizz-watcher --public --source=. --push
```

Then in the repo: **Settings → Secrets and variables → Actions → New
repository secret**, twice:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | your BotFather token |
| `TELEGRAM_CHAT_ID` | `590061033` |

Start it: **Actions** tab → *Fizz Utrecht watcher* → **Run workflow**.
The log should show `Watching FIZZ_UTRECHT every 20s (stint of 50 min)`.

## Option B: Railway (~$5/month)

One always-on process, no stint/handover juggling, no ToS gray area.
`railway.toml` sets `restartPolicyType = ALWAYS` (cloud equivalent of the
local supervisor).

1. Push to GitHub (private is fine here).
2. https://railway.app → New Project → Deploy from GitHub repo.
3. Service → **Variables**: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
   `FIZZ_HEADLESS=1`. Leave `FIZZ_MAX_RUNTIME_MINUTES` unset so it runs
   forever.

---

## Confirming it works

The daily Telegram check-in (~09:00 Amsterdam) now comes from the cloud.
If it arrives while your PC is off, the deployment is carrying it and you
can remove `fizz_watcher.vbs` from your Startup folder.

Until then, keep the PC watcher running — running both just means
duplicate alerts, which is harmless.

## Environment variables

| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token (overrides `fizz_config.json`) |
| `TELEGRAM_CHAT_ID` | Chat to alert |
| `FIZZ_HEADLESS` | `1` = no desktop alarm (required on any server) |
| `FIZZ_MAX_RUNTIME_MINUTES` | Exit after N minutes (GitHub Actions only) |
| `TZ` | Timezone for the daily check-in, e.g. `Europe/Amsterdam` |
