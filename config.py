import os

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_TELEGRAM_ID = int(os.environ["OWNER_TELEGRAM_ID"])

# The GitHub repo Railway will deploy for each panel, e.g. "yourname/vless-panel"
TARGET_REPO = os.environ["TARGET_REPO"]
TARGET_BRANCH = os.environ.get("TARGET_BRANCH") or None

MAX_PANELS_PER_ACCOUNT = int(os.environ.get("MAX_PANELS_PER_ACCOUNT", "2"))
HEALTH_SWEEP_INTERVAL_HOURS = float(os.environ.get("HEALTH_SWEEP_INTERVAL_HOURS", "24"))
