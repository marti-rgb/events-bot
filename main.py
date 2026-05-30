import asyncio
import os
import httpx
os.environ['TZ'] = 'Europe/Moscow'
import logging
from scheduler import start_scheduler
from database import has_session_today

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ADMIN_ID = '8231938381'

async def notify(text: str):
    if not TELEGRAM_TOKEN:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
                json={'chat_id': ADMIN_ID, 'text': text}
            )
    except Exception as e:
        logging.error(f"notify error: {e}")

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
