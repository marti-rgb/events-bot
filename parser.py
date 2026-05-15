import asyncio
import logging
from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, UsernameNotOccupiedError, FloodWaitError
from analyzer import analyze_post
from database import is_post_processed, mark_post_processed, save_event
from channels_config import CHANNELS, UNAVAILABLE_CHANNELS

API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'

async def parse_channel(client: TelegramClient, channel_config: dict, limit: int = 50):
    channel = channel_config['channel']
    threads = channel_config.get('threads')
    exclude_threads = channel_config.get('exclude_threads', [])
    
    saved = 0
    processed = 0
    
    try:
        entity = await client.get_entity(f'@{channel}')
    except (ChannelPrivateError, UsernameNotOccupiedError, ValueError) as e:
        logging.error(f"❌ Канал @{channel} недоступен: {e}")
        return 0, 0

    try:
        if threads:
            # Парсим только указанные треды
            for thread_id in threads:
                async for message in client.iter_messages(entity, reply_to=thread_id, limit=limit):
                    if not message.text:
                        continue
                    if is_post_processed(channel, message.id):
                        continue
                    
                    processed += 1
                    result = await analyze_post(message.text)
                    mark_post_processed(channel, message.id)
                    
                    if result and result.get('is_event'):
                        event = {
                            'title': result.get('title'),
                            'date': result.get('date'),
                            'time': result.get('time'),
                            'is_free': 1 if result.get('is_free') else 0,
                            'for_children': 1 if result.get('for_children') else 0,
                            'format': result.get('format', 'unknown'),
                            'category': result.get('category', 'другое'),
                            'description': result.get('description'),
                            'source_url': f'https://t.me/{channel}/{message.id}',
                            'channel': channel,
                            'city': 'Москва',
                        }
                        if save_event(event):
                            saved += 1
                            logging.info(f"✅ Сохранено: {event['title']} | {event['date']}")
                    
                    await asyncio.sleep(1.5)  # Rate limit Gemini
        else:
            # Парсим весь канал
            async for message in client.iter_messages(entity, limit=limit):
                if not message.text:
                    continue
                
                # Пропускаем исключённые треды
                if message.reply_to and hasattr(message.reply_to, 'reply_to_msg_id'):
                    if message.reply_to.reply_to_msg_id in exclude_threads:
                        continue
                
                if is_post_processed(channel, message.id):
                    continue
                
                processed += 1
                result = await analyze_post(message.text)
                mark_post_processed(channel, message.id)
                
                if result and result.get('is_event'):
                    event = {
                        'title': result.get('title'),
                        'date': result.get('date'),
                        'time': result.get('time'),
                        'is_free': 1 if result.get('is_free') else 0,
                        'for_children': 1 if result.get('for_children') else 0,
                        'format': result.get('format', 'unknown'),
                        'category': result.get('category', 'другое'),
                        'description': result.get('description'),
                        'source_url': f'https://t.me/{channel}/{message.id}',
                        'channel': channel,
                        'city': 'Москва',
                    }
                    if save_event(event):
                        saved += 1
                        logging.info(f"✅ Сохранено: {event['title']} | {event['date']}")
                
                await asyncio.sleep(1.5)

    except FloodWaitError as e:
        logging.warning(f"FloodWait {e.seconds}s для @{channel}")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        logging.error(f"Ошибка парсинга @{channel}: {e}")
    
    return processed, saved

async def run_parser(limit_per_channel: int = 20):
    logging.info("Начинаем парсинг...")
    logging.info(f"Недоступные каналы (приватные): {UNAVAILABLE_CHANNELS}")
    
    client = TelegramClient('events_session', API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        # Анонимный режим — только публичные каналы
        pass
    
    total_processed = 0
    total_saved = 0
    
    for channel_config in CHANNELS:
        channel = channel_config['channel']
        logging.info(f"Парсим @{channel}...")
        processed, saved = await parse_channel(client, channel_config, limit=limit_per_channel)
        total_processed += processed
        total_saved += saved
        logging.info(f"@{channel}: обработано {processed}, сохранено {saved}")
        await asyncio.sleep(3)  # Пауза между каналами
    
    await client.disconnect()
    logging.info(f"Парсинг завершён. Всего обработано: {total_processed}, сохранено мероприятий: {total_saved}")
    return total_processed, total_saved
