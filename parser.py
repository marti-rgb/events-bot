import asyncio
import logging
import httpx
from bs4 import BeautifulSoup
from analyzer import analyze_post
from database import is_post_processed, mark_post_processed, save_event
from sheets_config import load_channels, load_keywords, load_categories, match_category_l2, UNAVAILABLE_CHANNELS

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

async def fetch_channel_posts(client: httpx.AsyncClient, channel: str) -> list[dict]:
    url = f'https://t.me/s/{channel}'
    try:
        response = await client.get(url, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            logging.error(f"❌ @{channel} недоступен: HTTP {response.status_code}")
            return []
        
        soup = BeautifulSoup(response.text, 'html.parser')
        messages = soup.find_all('div', class_='tgme_widget_message')
        
        posts = []
        for msg in messages:
            msg_link = msg.get('data-post', '')
            if not msg_link:
                continue
            msg_id = msg_link.split('/')[-1]
            
            text_div = msg.find('div', class_='tgme_widget_message_text')
            text = text_div.get_text(separator='\n').strip() if text_div else ''
            
            if not text:
                continue
            
            date_tag = msg.find('time')
            post_date = date_tag.get('datetime', '')[:10] if date_tag else ''

            posts.append({
                'id': msg_id,
                'text': text,
                'url': f'https://t.me/{channel}/{msg_id}',
                'post_date': post_date
            })
        
        return posts
    except Exception as e:
        logging.error(f"❌ Ошибка получения @{channel}: {e}")
        return []

async def parse_channel(client: httpx.AsyncClient, channel_config: dict, filter_keywords: list = []):
    channel = channel_config['channel']
    exclude_ids = [str(i) for i in channel_config.get('exclude_threads', [])]
    whitelist_ids = [str(i) for i in (channel_config.get('threads') or [])]
    
    saved = 0
    processed = 0
    
    posts = await fetch_channel_posts(client, channel)
    
    if not posts:
        return 0, 0
    
    for post in posts:
        msg_id = post['id']
        
        if whitelist_ids and msg_id not in whitelist_ids:
            continue
        
        if msg_id in exclude_ids:
            continue
        
        if is_post_processed(channel, int(msg_id)):
            continue
        
        if filter_keywords:
            text_lower = post['text'].lower()
            if not any(kw.lower() in text_lower for kw in filter_keywords):
                mark_post_processed(channel, int(msg_id))
                continue
        
        processed += 1
        result = await analyze_post(post['text'], post.get('post_date', ''))
        mark_post_processed(channel, int(msg_id))
        
        if result and result.get('is_event') and result.get('date'):
            event = {
                'title': result.get('title'),
                'date': result.get('date'),
                'time': result.get('time'),
                'is_free': result.get('is_free'),
                'for_children': result.get('for_children', False),
                'format': result.get('format', 'unknown'),
                'category': result.get('category', 'другое'),
                'description': result.get('description'),
                'location': result.get('location'),
                'address': result.get('address'),
                'price': result.get('price'),
                'source_url': post['url'],
                'channel': channel,
                'city': channel_config.get('city', 'Москва'),
            }
            if save_event(event):
                saved += 1
                logging.info(f"✅ {event['title']} | {event['date']} | @{channel}")
        
        await asyncio.sleep(1.5)
    
    return processed, saved

async def run_parser():
    logging.info("Начинаем парсинг через t.me/s/...")
    
    CHANNELS = load_channels()
    FILTER_KEYWORDS = load_keywords()
    CATEGORIES = load_categories()
    
    logging.info(f"Каналов: {len(CHANNELS)}, ключевых слов: {len(FILTER_KEYWORDS)}")
    
    total_processed = 0
    total_saved = 0
    
    async with httpx.AsyncClient() as client:
        for channel_config in CHANNELS:
            channel = channel_config['channel']
            logging.info(f"Парсим @{channel}...")
            processed, saved = await parse_channel(client, channel_config, FILTER_KEYWORDS)
            total_processed += processed
            total_saved += saved
            logging.info(f"@{channel}: обработано {processed}, сохранено {saved}")
            await asyncio.sleep(2)
    
    logging.info(f"Готово. Обработано: {total_processed}, сохранено: {total_saved}")
    return total_processed, total_saved
