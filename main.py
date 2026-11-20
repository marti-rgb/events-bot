# main.py — v3
# v3 (2026-11-20): Let the persistent parser pass decide whether a backup run is needed.
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
    await start_scheduler()

if __name__ == "__main__":
    asyncio.run(main())
