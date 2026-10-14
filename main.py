# main.py — v2
# v2 — notify() moved to notify.py (parser.py uses it too); behaviour unchanged. Updated 14.10.2026
import asyncio
import os
os.environ['TZ'] = 'Europe/Moscow'
import logging
from scheduler import start_scheduler
from database import has_session_today
from notify import notify

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


async def main():
    logging.info("Starting Events Bot...")
    is_backup = os.environ.get('IS_BACKUP_RUN') == '1'
    if is_backup:
        if has_session_today():
            logging.info("Сессия сегодня уже была. Резервный запуск не нужен.")
            return
        await notify("⚠️ Основной парсер не запустился сегодня. Запускаю резервный.")
    await start_scheduler()

if __name__ == "__main__":
    asyncio.run(main())
