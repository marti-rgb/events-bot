import logging
from database import init_db
from parser import run_parser

async def start_scheduler():
    init_db()
    logging.info("БД инициализирована")
    await run_parser()
