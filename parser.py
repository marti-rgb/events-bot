# parser.py — ВЕРСИЯ v49 — 14.10.2026
# v49 — справочник категорий: Google-лист → копия в базе (categories_catalog, обновляется после каждого удачного
# чтения листа) → вшитый список в коде; фильтр категорий больше не отключается. Справочник не из листа или копия
# в базе не обновилась — сообщение админу (notify.py). Updated 14.10.2026
# v48 — 28.08.2026: текст поста, который нейросеть сочла НЕ событием, дополнительно сохраняется в таблицу
# rejected_posts — чтобы искать новые стоп-слова частотным анализом по базе.
#
# История версий (старше двух последних) — в versions_history.md
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
from database import load_categories_db, save_categories_db
from sheets_config import load_channels, load_keywords, load_categories, match_category_l2, load_stop_tags, UNAVAILABLE_CHANNELS, CATEGORIES_FALLBACK
from notify import notify
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


# ===== patch 1 (v49): справочник категорий never empty =====
# Google sheet (the user edits it) → its copy in the database → the list built into the code.
# The sheet answered -> the copy in the database is overwritten (the bots' catalog uses it).
# Anything but a clean sheet read -> one message to the admin per run.
async def load_categories_safe() -> dict:
    cats = load_categories()
    problem = None
    if cats:
        saved = save_categories_db(cats)
        if saved is False:
            problem = 'лист Google прочитан, но копию справочника в базе обновить не удалось (база не ответила). Каталог в ботах работает по старой копии.'
        elif saved is None:
            problem = f'в листе Google всего {len(cats)} разделов — похоже на сбой чтения, копию в базе не трогал. Прогон идёт по этому листу.'
        source = 'лист Google'
    else:
        cats = load_categories_db()
        source = 'копия в базе (лист Google не ответил)'
        if not cats:
            cats = {k: list(v) for k, v in CATEGORIES_FALLBACK.items()}
            source = 'вшитый список в коде (не ответили ни лист Google, ни база)'
        problem = (f'справочник категорий взят не из листа Google.\nИсточник: {source}, разделов: {len(cats)}.\n'
                   'Если вы недавно меняли лист — новые категории в этот раз не учтутся.')
        if source.startswith('вшитый'):
            problem += '\nПодкатегории в этом прогоне не сохраняются.'
    logging.warning(f"Справочник категорий: {source}, разделов {len(cats)}")
    if problem:
        await notify(f"⚠️ Парсер: {problem}\nПрогон идёт, мероприятия не теряются.")
    return cats


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
    CATEGORIES = await load_categories_safe()  # patch 1 (v49)
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
