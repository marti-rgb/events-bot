# notify.py — v2
# v2 (2026-11-27): Distinguish accepted, rejected and uncertain Telegram delivery.
# v1 — one place to message the admin in Telegram; used by main.py and parser.py (moved out of main.py: parser.py cannot import main.py). Updated 14.10.2026
import os
import logging
import httpx

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ADMIN_ID = '8231938381'


async def notify(text: str) -> bool | None:
    """True if accepted, False if rejected, None if delivery is uncertain."""
    if not TELEGRAM_TOKEN:
        logging.warning(f"notify: no TELEGRAM_BOT_TOKEN, message not sent: {text[:200]}")
        return False
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                json={'chat_id': ADMIN_ID, 'text': text[:4000]},
                timeout=15
            )
            if r.status_code == 200:
                return True
            logging.error(f"notify refused: HTTP {r.status_code} {r.text[:300]}")
            return False if 400 <= r.status_code < 500 else None
    except Exception as e:
        logging.error(f"notify error: {type(e).__name__}")
        return None
