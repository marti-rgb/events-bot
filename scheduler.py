import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from database import init_db
from parser import run_parser

async def scheduled_parse():
    logging.info("Запуск по расписанию...")
    await run_parser()

async def start_scheduler():
    init_db()
    logging.info("БД инициализирована")
    
    # Первый запуск сразу при старте
    await run_parser()
    
    # Далее каждые 6 часов
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled_parse, 'interval', hours=6)
    scheduler.start()
    
    logging.info("Планировщик запущен. Парсинг каждые 6 часов.")
    
    # Держим процесс живым
    while True:
        await asyncio.sleep(3600)
