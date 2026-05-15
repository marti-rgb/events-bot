import asyncio
import logging
from scheduler import start_scheduler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

async def main():
    logging.info("Starting Events Bot...")
    await start_scheduler()

if __name__ == "__main__":
    asyncio.run(main())
