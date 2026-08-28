# ВЕРСИЯ v48 — 28.08.2026
# Изменено относительно v47: текст поста, который нейросеть сочла НЕ событием,
# теперь дополнительно сохраняется в таблицу rejected_posts (не только в
# консольный лог) — чтобы искать новые стоп-слова частотным анализом по базе,
# а не вручную по логам отдельных прогонов.
# Изменено относительно v46: добавлено логирование ОРИГИНАЛЬНОГО текста поста
# (не пересказа модели), когда модель сочла его НЕ событием — чтобы искать
# реальные паттерны для расширения списка ключевых слов/стоп-тегов.
# Изменено относительно v45: добавлено логирование, когда пост отсеивается
# стоп-тегом — видно, каким именно тегом и какой текст отсеян, чтобы можно
# было проверять по факту, не режет ли какой-то стоп-тег настоящие события.
# Изменено относительно v44: пост помечается обработанным (mark_post_processed)
# ТОЛЬКО если модель реально вернула результат. Раньше помечался всегда, даже
# при полном отказе и Z.ai, и Groq одновременно — такой пост терялся навсегда,
# следующий прогон его больше не трогал. Теперь при полном отказе пост остаётся
# в очереди и будет предпринята повторная попытка на следующем прогоне.
# Изменено относительно v41: фильтр постов по дате публикации (PARSE_SINCE):
# посты старше указанной даты пропускаются без вызова модели.
# Изменено относительно v42: мягкая остановка по времени (MAX_RUN_MINUTES) —
# при подходе к лимиту GitHub Actions прогон завершается сам, зелёной галочкой,
# а не обрывается по Cancelled.
# Изменено относительно v40: Cerebras отключён, первый провайдер Z.ai (glm-4.5-flash),
# запасные — Groq openai/gpt-oss-120b и openai/gpt-oss-20b; переключатель размышлений;
# 429 без длинной паузы; правки промпта (цена, дата, диапазон дат);
# честный лог первой линии; листание истории каналов; деление каналов на части;
# первая линия отключаема (USE_SCREEN).
# Переменные в GitHub → Settings → Variables:
#   ANALYZE_THINKING=0  PARSE_PAGES=1  USE_SCREEN=0  CHANNELS_PART=(пусто)
#   PARSE_SINCE=(пусто) — нижняя граница по дате публикации поста, ГГГГ-ММ-ДД
import asyncio
import logging
import time
import httpx
from bs4 import BeautifulSoup
from analyzer import analyze_post, screen_post
from database import log_parse, start_parse_session, finish_parse_session
from database import is_post_processed, mark_post_processed, save_event, log_rejected_post
from sheets_config import load_channels, load_keywords, load_categories, match_category_l2, load_stop_tags, UNAVAILABLE_CHANNELS
import os

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

# Сколько страниц истории канала листать назад. 1 страница ≈ 20 постов.
# Управляется переменной PARSE_PAGES в GitHub → Settings → Variables.
try:
    PARSE_PAGES = max(1, min(int(os.getenv('PARSE_PAGES', '1') or 1), 20))
except ValueError:
    PARSE_PAGES = 1

# Нижняя граница по дате публикации поста, формат ГГГГ-ММ-ДД.
# Посты старше пропускаются мгновенно, без вызова модели. Пусто — берём все.
PARSE_SINCE = (os.getenv('PARSE_SINCE') or '').strip()

# Мягкий бюджет времени на весь прогон, в минутах. Как подойдём к границе —
# останавливаемся сами (зелёная галочка), не дожидаясь принудительной отмены GitHub.
try:
    MAX_RUN_MINUTES = int(os.getenv('MAX_RUN_MINUTES', '320') or 320)
except ValueError:
    MAX_RUN_MINUTES = 320

# Первая линия (быстрая проверка "событие / не событие" перед разбором).
# У Z.ai короткий ответ стоит столько же времени, сколько полный разбор,
# поэтому по умолчанию выключена — разбор сам отсеивает не-события.
# Включается переменной USE_SCREEN=1 в GitHub → Settings → Variables.
USE_SCREEN = (os.getenv('USE_SCREEN', '0') or '0').strip().lower() in ('1', 'true', 'yes', 'on')


async def fetch_channel_posts(client: httpx.AsyncClient, channel: str, pages: int = None) -> list[dict]:
    pages = PARSE_PAGES if pages is None else max(1, pages)
    all_posts = []
    seen_ids = set()
    before = None

    for page in range(pages):
        url = f'https://t.me/s/{channel}'
        if before:
            url += f'?before={before}'

        page_posts = await _fetch_one_page(client, channel, url)
        if not page_posts:
            break

        new_posts = [p for p in page_posts if p['id'] not in seen_ids]
        if not new_posts:
            break

        for p in new_posts:
            seen_ids.add(p['id'])
        all_posts.extend(new_posts)

        try:
            before = min(int(p['id']) for p in new_posts)
        except ValueError:
            break
        if before <= 1:
            break

    if pages > 1:
        logging.info(f"@{channel}: собрано {len(all_posts)} постов за {pages} стр.")
    return all_posts


async def _fetch_one_page(client: httpx.AsyncClient, channel: str, url: str) -> list[dict]:
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

        if PARSE_SINCE:
            post_date_val = post.get('post_date') or ''
            if not post_date_val or post_date_val < PARSE_SINCE:
                continue
        
        if is_post_processed(channel, int(msg_id)):
            continue
        
        if filter_keywords:
            text_lower = post['text'].lower()
            if not any(kw.lower() in text_lower for kw in filter_keywords):
                mark_post_processed(channel, int(msg_id))
                continue

        if stop_tags:
            text_lower = post['text'].lower()
            matched = next((tag for tag in stop_tags if tag in text_lower), None)
            if matched:
                snippet = post['text'][:150].strip().replace('\n', ' ')
                logging.info(f"🚫 @{channel}/{msg_id} отсеян стоп-тегом '{matched}': {snippet!r}")
                mark_post_processed(channel, int(msg_id))
                continue

        link_count = post['text'].lower().count('timepad') + post['text'].lower().count('bilet.mos')
        if link_count >= 3:
            mark_post_processed(channel, int(msg_id))
            skipped += 1
            continue

        if USE_SCREEN:
            is_event, screen_model = await screen_post(post['text'])
            log_parse(
                channel=channel,
                post_id=str(msg_id),
                model=screen_model or 'none',
                fallback=bool(screen_model and not screen_model.startswith('zai/')),
                success=screen_model is not None,
                stage='screen',
            )
            if not is_event:
                mark_post_processed(channel, int(msg_id))
                skipped += 1
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
            stage='analyze',
        )
        if result:
            mark_post_processed(channel, int(msg_id))

        if result and not result.get('is_event'):
            snippet = post['text'][:200].strip().replace('\n', ' ')
            logging.info(f"❌ @{channel}/{msg_id} НЕ событие (оригинал поста): {snippet!r}")
            log_rejected_post(channel, str(msg_id), post['text'])

        if result and result.get('is_event') and result.get('date'):
            event = {
                'title': result.get('title'),
                'date': result.get('date'),
                'time': result.get('time'),
                'is_free': result.get('is_free'),
                'for_children': result.get('for_children', False),
                'format': result.get('format', 'unknown'),
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

    # Обработать только часть списка каналов. Формат "N/M", например "2/5" —
    # вторая пятая часть. Пусто или "1/1" — все каналы (обычный режим).
    # Управляется переменной CHANNELS_PART в GitHub → Settings → Variables.
    part = (os.getenv('CHANNELS_PART') or '').strip()
    if part and '/' in part:
        try:
            n_str, m_str = part.split('/', 1)
            n, m = int(n_str), int(m_str)
            if m > 1 and 1 <= n <= m:
                total = len(CHANNELS)
                CHANNELS = CHANNELS[(n - 1) * total // m: n * total // m]
                logging.info(f"Часть {n} из {m}: берём {len(CHANNELS)} каналов из {total}")
        except ValueError:
            logging.warning(f"CHANNELS_PART='{part}' — не понял формат, беру все каналы")

    FILTER_KEYWORDS = load_keywords()
    CATEGORIES = load_categories()
    STOP_TAGS = load_stop_tags()
    
    logging.info(f"Каналов: {len(CHANNELS)}, ключевых слов: {len(FILTER_KEYWORDS)}")
    if PARSE_SINCE:
        logging.info(f"Берём только посты от {PARSE_SINCE} и новее")
    
    total_fetched = 0
    total_processed = 0
    total_saved = 0
    total_skipped = 0
    total_error = 0

    github_run_id = os.environ.get('GITHUB_RUN_ID')
    github_run_url = f"https://github.com/marti-rgb/events-bot/actions/runs/{github_run_id}" if github_run_id else None
    session_id = start_parse_session(github_run_id, github_run_url)
    
    run_started = time.monotonic()
    async with httpx.AsyncClient() as client:
        for idx, channel_config in enumerate(CHANNELS):
            elapsed_min = (time.monotonic() - run_started) / 60
            if elapsed_min > MAX_RUN_MINUTES:
                left = len(CHANNELS) - idx
                logging.warning(
                    f"Мягкая остановка по времени: прошло {elapsed_min:.0f} мин "
                    f"(лимит {MAX_RUN_MINUTES}). Необработанных каналов: {left} из {len(CHANNELS)}, "
                    f"первый из них — @{channel_config['channel']}"
                )
                break
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
