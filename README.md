# Railway VPN Panel Deploy Bot

A Telegram bot that deploys the [vless-panel](../vless-panel) project onto your
own Railway accounts, and manages the results — up to 2 panels per account,
password rotation, redeploys, and a 24h health sweep.

Only responds to one Telegram user ID (`OWNER_TELEGRAM_ID`) — everyone else is
silently ignored.

## 1. Create the Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` →
   follow the prompts → copy the token it gives you.
2. Message [@userinfobot](https://t.me/userinfobot) to get your own numeric
   Telegram user ID.

## 2. Generate an encryption key

Railway account tokens are encrypted at rest with this key. Run locally:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Save the output — you'll set it as `ENCRYPTION_KEY`. **Keep the same key on
every future redeploy of this bot**, or old backups/data won't decrypt.

## 3. Push this repo to GitHub

```bash
cd railway-vpn-bot
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/<you>/railway-vpn-bot.git
git push -u origin main
```

## 4. Deploy the bot itself on Railway

1. Railway → **New Project → Deploy from GitHub repo** → pick this repo.
   Railway reads the `Procfile` (`worker: python bot.py`) automatically —
   this is a background worker, it doesn't need a public domain.
2. Service → **Variables** → add:
   - `TELEGRAM_BOT_TOKEN` — from step 1
   - `OWNER_TELEGRAM_ID` — from step 1
   - `TARGET_REPO` — the GitHub repo of the *panel* project, e.g.
     `yourname/vless-panel` (this is what gets deployed when you tap
     "Deploy Panel" in the bot — push that project to its own repo first
     if you haven't)
   - `ENCRYPTION_KEY` — from step 2
   - Optional: `MAX_PANELS_PER_ACCOUNT` (default `2`),
     `HEALTH_SWEEP_INTERVAL_HOURS` (default `24`)
3. Deploy. Check **Logs** for `bot starting (polling)`.
4. Message your bot `/start` on Telegram.

## 5. Add a Railway account to the bot

You'll need an **account-level** API token (not a project token) so the bot
can create new projects on your behalf:
[railway.com/account/tokens](https://railway.com/account/tokens) → create
token → copy it.

In the bot: `/start` → `👤 Accounts` → `➕ Add Account` → paste the token →
give it a label. The bot validates it live before saving.

## 6. Deploy a panel

`/start` → `🚀 Deploy Panel` → pick the account → wait. You'll get back the
panel's URL and its admin password when it finishes. Progress is shown live
in the same message as it moves through each step.

## Notes

- **Bot data persistence**: like the panel itself, this bot's own SQLite DB
  sits on Railway's ephemeral disk and can be wiped on redeploy. Use
  `💾 Backup / Import` periodically — export saves a JSON file with all
  accounts (tokens still encrypted) and panels; import restores it on a
  fresh deploy, provided `ENCRYPTION_KEY` matches.
- **Health sweep**: runs every `HEALTH_SWEEP_INTERVAL_HOURS` (default 24h)
  and only messages you when a panel's or account's status *changes* —
  not on every sweep — so it won't spam you while something stays down.
  Mute alerts for a specific panel from its detail view if you don't want
  it checked at all.
- **Account deletion**: if the account still has panels, you're asked
  whether to delete those panels' Railway projects too, or just remove the
  account from the bot and leave them running.
