import asyncio
import logging
import httpx
from bs4 import BeautifulSoup
from analyzer import analyze_post
from database import log_parse, start_parse_session, finish_parse_session
from database import is_post_processed, mark_post_processed, save_event
from sheets_config import load_channels, load_keywords, load_categories, match_category_l2, load_stop_tags, UNAVAILABLE_CHANNELS

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

async def parse_channel(client: httpx.AsyncClient, channel_config: dict, filter_keywords: list = [], categories: dict = {}, stop_tags: list = []):
    channel = channel_config['channel']
    exclude_ids = [str(i) for i in channel_config.get('exclude_threads', [])]
    whitelist_ids = [str(i) for i in (channel_config.get('threads') or [])]
    
    fetched = 0
    saved = 0
    processed = 0
    skipped = 0
    error = 0

    posts = await fetch_channel_posts(client, channel)
    if not posts:
        return 0, 0, 0, 0, 0
    fetched = len(posts)
    
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
        if stop_tags and any(tag in post['text'].lower() for tag in stop_tags):
            mark_post_processed(channel, int(msg_id))
            continue

        skipped += 1
            mark_post_processed(channel, int(msg_id))
            continue
        
        processed += 1
        result = await analyze_post(post['text'], post.get('post_date', ''), categories)
        if not result:
            error += 1
        log_parse(
            channel=channel,
            post_id=str(msg_id),
            model=result.get('model') if result else None,
            fallback=result.get('model', '').startswith('groq') if result else False,
            success=bool(result),
            category_l1_arr=result.get('category_l1_arr', []) if result else [],
            category_l2_arr=result.get('category_l2_arr', []) if result else [],
        )
        mark_post_processed(channel, int(msg_id))
        
        if result and result.get('is_event') and result.get('date'):
            event = {
                'title': result.get('title'),
                'date': result.get('date'),
                'time': result.get('time'),
                'is_free': result.get('is_free'),
                'for_children': result.get('for_children', False),
                'format': result.get('format', 'unknown'),
                'category_l1': result.get('category_l1', 'другое'),
                'category_l2': match_category_l2(result.get('category_l2', ''), categories, result.get('category_l1', '')),
                'description': result.get('description'),
                'location': result.get('location'),
                'city_parsed': result.get('city_parsed'),
                'address': result.get('address'),
                'price': result.get('price'),
                'source_url': post['url'],
                'channel': channel,
                'city': channel_config.get('city', 'Москва'),
                'category_l1_arr': result.get('category_l1_arr', []),
               'category_l2_arr': result.get('category_l2_arr', []),
                'model': result.get('model'),
            }
            if save_event(event):
                if len(result.get('category_l1_arr', [])) >= 16:
                    mark_post_processed(channel, int(msg_id))
                    continue
                saved += 1
                logging.info(f"✅ {event['title']} | {event['date']} | @{channel}")
        
        await asyncio.sleep(1.5)
    
    return fetched, processed, saved, skipped, error

async def run_parser():
    logging.info("Начинаем парсинг через t.me/s/...")
    
    CHANNELS = load_channels()
    FILTER_KEYWORDS = load_keywords()
    CATEGORIES = load_categories()
    STOP_TAGS = load_stop_tags()
    
    logging.info(f"Каналов: {len(CHANNELS)}, ключевых слов: {len(FILTER_KEYWORDS)}")
    
    total_fetched = 0
    total_processed = 0
    total_saved = 0
    total_skipped = 0
    total_error = 0

    github_run_id = os.environ.get('GITHUB_RUN_ID')
    github_run_url = f"https://github.com/marti-rgb/events-bot/actions/runs/{github_run_id}" if github_run_id else None
    session_id = start_parse_session(github_run_id, github_run_url)
    
    async with httpx.AsyncClient() as client:
        for channel_config in CHANNELS:
            channel = channel_config['channel']
            logging.info(f"Парсим @{channel}...")
            fetched, processed, saved, skipped, error = await parse_channel(client, channel_config, FILTER_KEYWORDS, CATEGORIES, STOP_TAGS)
            total_fetched += fetched
            total_processed += processed
            total_saved += saved
            total_skipped += skipped
            total_error += error
            logging.info(f"@{channel}: получено {fetched}, обработано {processed}, сохранено {saved}")
            await asyncio.sleep(2)
    if session_id:
        finish_parse_session(session_id, {
            'fetched': total_fetched,
            'processed': total_processed,
            'saved': total_saved,
            'skipped': total_skipped,
            'error': total_error,
            'channels': len(CHANNELS)
        })
    logging.info(f"Готово. Получено: {total_fetched}, обработано: {total_processed}, сохранено: {total_saved}")
    return total_processed, total_saved
