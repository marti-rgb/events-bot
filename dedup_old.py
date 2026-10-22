# dedup_old.py — v1
# v1 — task 56: entry point of the daily duplicate check of old events (GitHub workflow dedup_old.yml).
#      All the logic lives in dedup.py (run_old); this file only sets the time zone, logging and calls it.
#      Created 22.10.2026
import os
os.environ['TZ'] = 'Europe/Moscow'
import time
time.tzset()
import asyncio
import logging

from notify import notify
import dedup

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


async def main():
    logging.info("Проверка старых мероприятий на дубли — старт")
    await dedup.run_old(notify)
    logging.info("Проверка старых мероприятий на дубли — готово")


if __name__ == "__main__":
    asyncio.run(main())
