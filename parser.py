# parser.py — v56
# v56 (2026-11-27): Send new review links once and resume unfinished retries on the backup run.
# v55 (2026-11-27): Continue the channel pass and replay unfinished posts from a persistent queue.
# История версий (старше двух последних) — в versions_history.md
# Переменные в GitHub → Settings → Variables:
#   ANALYZE_THINKING=0  PARSE_PAGES=1  USE_SCREEN=0  CHANNELS_PART=(пусто)
#   PARSE_SINCE=(пусто) — нижняя граница по дате публикации поста, ГГГГ-ММ-ДД
#   DUP_ON=1 — поиск дублей (0 — выключить, парсер работает как раньше)
#   DUP_MODEL=@cf/baai/bge-m3   DUP_FILL_MAX=400 — сколько старых мероприятий получает вектор за прогон
import asyncio
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import httpx
from bs4 import BeautifulSoup
from analyzer import IncompleteEventError, analyze_post, screen_post
from database import log_parse, start_parse_session, finish_parse_session, ParserPass
from database import mark_post_processed, save_event, log_rejected_post
from database import load_categories_db, save_categories_db
import post_retries
from sheets_config import load_channels, load_keywords, load_categories, match_category_l2, load_stop_tags, UNAVAILABLE_CHANNELS, CATEGORIES_FALLBACK
from notify import notify
import os

# patch 2 (v50): duplicates live in their own file. A broken dedup.py must not stop the parser,
# so even the import is guarded — without it the parser simply works the way it did before.
try:
    import dedup
except Exception as _dedup_import_error:
    dedup = None
    logging.error(f"dedup.py не загрузился, поиск дублей выключен: {_dedup_import_error}")

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}
MOSCOW = ZoneInfo('Europe/Moscow')

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
            raise RuntimeError(f'@{channel}: HTTP {response.status_code}')
        
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
            
            posts.append({
                'id': msg_id,
                'text': text,
                'url': f'https://t.me/{channel}/{msg_id}',
                'post_date': publication_date(msg),
                'links': post_links(text_div),
            })
        
        return posts
    except Exception as e:
        logging.error(f"❌ Ошибка получения @{channel}: {e}")
        raise


def publication_date(message):
    # The footer belongs to this post. Dates in the text or a reply can refer to other events.
    selectors = (
        '.tgme_widget_message_date time[datetime]',
        '.tgme_widget_message_footer time[datetime]',
        'time[datetime]',
    )
    for selector in selectors:
        for tag in message.select(selector):
            parent = tag.find_parent('div', class_='tgme_widget_message')
            if parent is not message or tag.find_parent(class_=(
                'tgme_widget_message_text', 'tgme_widget_message_reply', 'tgme_widget_message_forwarded_from',
            )):
                continue
            try:
                stamp = datetime.fromisoformat(tag['datetime'])
            except (ValueError, TypeError):
                continue
            return (stamp.astimezone(MOSCOW) if stamp.tzinfo else stamp).date().isoformat()
    return ''


async def fetch_embedded_publication_date(client, channel, msg_id):
    url = f'https://t.me/{channel}/{msg_id}?embed=1&mode=tme'
    try:
        response = await client.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            page = BeautifulSoup(response.text, 'html.parser')
            target = f'{channel}/{msg_id}'.casefold()
            message = next((msg for msg in page.select('div.tgme_widget_message[data-post]')
                            if msg['data-post'].casefold() == target), None)
            if message:
                return publication_date(message)
        else:
            logging.warning(f'@{channel}/{msg_id}: дата публикации недоступна в отдельном посте, HTTP {response.status_code}')
    except httpx.HTTPError as exc:
        logging.warning(f'@{channel}/{msg_id}: ошибка чтения отдельного поста, {type(exc).__name__}')
    return ''


def post_links(text_div) -> dict[str, str]:
    links = {}
    for a in text_div.find_all('a', href=True):
        url = (a['href'] or '').strip()
        if not url or len(url) > 500 or any(char.isspace() or ord(char) < 32 or char in '"<>\\' for char in url):
            continue
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None:
                continue
        except ValueError:
            continue
        links.setdefault(url, a.get_text(separator=' ', strip=True)[:100])
        if len(links) >= 12:
            break
    return links

def _hold_post(channel, post_id, stage, reason, attempts, replay, *, incomplete=False, pass_key=None):
    if replay:
        post_retries.finish_retry(channel, post_id, 'review' if incomplete else 'pending', stage, reason, attempts,
                                  pass_key=pass_key)
    else:
        post_retries.defer_post(channel, post_id, stage, reason, attempts)


async def parse_channel(client: httpx.AsyncClient, channel_config: dict, filter_keywords: list = [], categories: dict = {}, stop_tags: list = [], *, replay_post=None, replay_attempts=None, retry_pass=None):
    channel = channel_config['channel']
    exclude_ids = [str(i) for i in channel_config.get('exclude_threads', [])]
    whitelist_ids = [str(i) for i in (channel_config.get('threads') or [])]
    
    fetched = 0
    saved = 0
    processed = 0
    skipped = 0
    error = 0
    deferred = 0

    posts = [replay_post] if replay_post is not None else await fetch_channel_posts(client, channel)
    fetched = len(posts)
    
    for post in posts:
        msg_id = post['id']
        
        if whitelist_ids and msg_id not in whitelist_ids:
            continue
        
        if msg_id in exclude_ids:
            continue

        if replay_post is None:
            post_date_val = post.get('post_date') or ''
            if PARSE_SINCE and post_date_val and post_date_val < PARSE_SINCE:
                continue
            if post_retries.is_known_post(channel, msg_id):
                continue
            if PARSE_SINCE and not post_date_val:
                post_date_val = await fetch_embedded_publication_date(client, channel, msg_id)
                if not post_date_val or post_date_val < PARSE_SINCE:
                    continue
                post['post_date'] = post_date_val
        
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

        attempts = replay_attempts if replay_attempts is not None else []
        if USE_SCREEN:
            is_event, screen_model = await screen_post(post['text'], attempts=attempts)
            log_parse(
                channel=channel,
                post_id=str(msg_id),
                model=screen_model or 'none',
                fallback=bool(screen_model and not screen_model.startswith('zai/')),
                success=screen_model is not None,
                stage='screen', qualified=False if screen_model is not None and not is_event else None, metrics_version=1,
            )
            if screen_model is None:
                _hold_post(channel, msg_id, 'screen', 'Предварительный отбор не завершился', attempts,
                           replay_post is not None, pass_key=retry_pass)
                error += 1
                continue
            if not is_event:
                mark_post_processed(channel, int(msg_id))
                skipped += 1
                continue

        if not post.get('post_date'):
            post['post_date'] = await fetch_embedded_publication_date(client, channel, msg_id)
        if replay_post is not None:
            logging.info(f"@{channel}/{msg_id}: дата публикации {post['post_date'] or 'не найдена'}")

        processed += 1
        try:
            result = await analyze_post(post['text'], post.get('post_date', ''), categories, post.get('links'),
                                        attempts=attempts)
        except IncompleteEventError as exc:
            post_url = post['url']
            log_parse(
                channel=channel,
                post_id=str(msg_id),
                model=None,
                fallback=False,
                success=False,
                error=f'Отложено на разбор: нет названия или даты. {post_url}',
                stage='analyze',
                metrics_version=1,
            )
            fields = {'title': 'название', 'date': 'дата'}
            reason = 'Не хватает данных: ' + ', '.join(fields[field] for field in exc.missing)
            _hold_post(channel, msg_id, 'analyze', reason, attempts, replay_post is not None,
                       incomplete=True, pass_key=retry_pass)
            deferred += 1
            logging.warning(f"Отложено на разбор: {reason.lower()}. {post_url}")
            await asyncio.sleep(1.5)
            continue
        if not result:
            error += 1
            too_short = bool(attempts and attempts[-1]['модель'] == 'local')
            reason = 'В посте меньше 20 символов текста' if too_short else 'Модели не завершили разбор поста'
            _hold_post(channel, msg_id, 'analyze', reason, attempts, replay_post is not None,
                       incomplete=too_short, pass_key=retry_pass)
        log_parse(
            channel=channel,
            post_id=str(msg_id),
            model=result.get('model') if result else None,
            fallback=result.get('model', '').startswith('groq') if result else False,
            success=bool(result),
            category_l1_arr=result.get('category_l1_arr', []) if result else [],
            category_l2_arr=result.get('category_l2_arr', []) if result else [],
            stage='analyze',
            qualified=(bool(result.get('is_event') and result.get('date')) if result else None), metrics_version=1,
        )
        if result and not result.get('is_event'):
            snippet = post['text'][:200].strip().replace('\n', ' ')
            logging.info(f"❌ @{channel}/{msg_id} НЕ событие (оригинал поста): {snippet!r}")
            log_rejected_post(channel, str(msg_id), post['text'])
            mark_post_processed(channel, int(msg_id))

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
                'has_registration': result.get('has_registration') is True,
                'reg_link': result.get('reg_link'),
                'source_url': post['url'],
                'channel': channel,
                'city': channel_config.get('city', 'Москва'),
                'category_l1_arr': result.get('category_l1_arr', []),
                'category_l2_arr': result.get('category_l2_arr', []),
                'model': result.get('model'),
            }
            new_event_id = save_event(event)  # v5: the id of the saved event, None if it was not saved
            mark_post_processed(channel, int(msg_id))
            if new_event_id:
                if len(result.get('category_l1_arr', [])) >= 16:
                    mark_post_processed(channel, int(msg_id))
                    continue
                saved += 1
                logging.info(f"✅ {event['title']} | {event['date']} | @{channel}")
                # patch 2 (v50): semantic duplicate check. Nothing is hidden while dup_auto is off —
                # pairs are only put in the queue for the admin to confirm in the add-bot.
                if dedup is not None:
                    try:
                        await dedup.process(new_event_id, event)
                    except Exception as e:
                        logging.error(f"Проверка дублей не прошла (мероприятие {new_event_id}): {e}")
        
        await asyncio.sleep(1.5)
    
    return fetched, processed, saved, skipped, error, deferred


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


async def _run_parser_pass(progress, deadline, all_channels):
    logging.info("Начинаем парсинг через t.me/s/...")
    CHANNELS = all_channels

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

    CHANNELS = progress.begin(CHANNELS, os.getenv('IS_BACKUP_RUN') == '1')
    if CHANNELS is None:
        logging.info('Обход каналов сегодня уже завершён')
        return None
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
    channels_done = 0

    github_run_id = os.environ.get('GITHUB_RUN_ID')
    github_run_url = f"https://github.com/marti-rgb/events-bot/actions/runs/{github_run_id}" if github_run_id else None
    session_id = start_parse_session(github_run_id, github_run_url)
    
    async with httpx.AsyncClient() as client:
        for idx in range(progress.next_index, len(CHANNELS)):
            channel_config = CHANNELS[idx]
            if time.monotonic() >= deadline:
                left = len(CHANNELS) - idx
                logging.warning(
                    f"Мягкая остановка по времени (лимит {MAX_RUN_MINUTES} мин). "
                    f"Необработанных каналов: {left} из {len(CHANNELS)}, "
                    f"первый из них — @{channel_config['channel']}"
                )
                break
            channel = channel_config['channel']
            logging.info(f"Парсим @{channel}...")
            fetched, processed, saved, skipped, error, deferred = await parse_channel(
                client, channel_config, FILTER_KEYWORDS, CATEGORIES, STOP_TAGS,
            )
            total_fetched += fetched
            total_processed += processed
            total_saved += saved
            total_skipped += skipped
            total_error += error + deferred
            progress.advance(idx + 1)
            channels_done += 1
            logging.info(f"@{channel}: получено {fetched}, обработано {processed}, сохранено {saved}")
            await asyncio.sleep(2)

    if session_id:
        finish_parse_session(session_id, {
            'fetched': total_fetched,
            'processed': total_processed,
            'saved': total_saved,
            'skipped': total_skipped,
            'error': total_error,
            'channels': channels_done
        })
    logging.info(f"Готово. Получено: {total_fetched}, обработано: {total_processed}, сохранено: {total_saved}")

    settings = (all_channels, FILTER_KEYWORDS, CATEGORIES, STOP_TAGS)
    return total_processed, total_saved, settings


async def _retry_posts(settings, deadline, *, include_review, pass_key):
    channels, keywords, categories, stop_tags = settings
    channels = {config['channel']: config for config in channels}
    processed = 0
    saved = 0
    async with httpx.AsyncClient() as client:
        for row in post_retries.retry_targets(include_review, pass_key):
            if time.monotonic() >= deadline:
                return processed, saved, False
            channel, post_id = row['channel'], row['post_id']
            url = f'https://t.me/{channel}/{post_id}'
            attempts = []
            if row['already_saved']:
                post_retries.finish_retry(channel, post_id, 'resolved', 'save', 'Карточка уже в базе', attempts,
                                          pass_key=pass_key)
                continue
            if channel not in channels:
                post_retries.finish_retry(channel, post_id, 'review', 'fetch', 'Канала нет в списке парсера', attempts,
                                          pass_key=pass_key)
                continue
            try:
                page = await _fetch_one_page(client, channel, f'https://t.me/s/{channel}?before={int(post_id) + 1}')
                post = next((item for item in page if item['id'] == post_id), None)
                if post is None:
                    post_retries.finish_retry(channel, post_id, 'review', 'fetch', 'Пост недоступен в веб-версии Telegram',
                                              attempts, pass_key=pass_key)
                    continue
                _, tried, added, _, error, deferred = await parse_channel(
                    client, channels[channel], keywords, categories, stop_tags,
                    replay_post=post, replay_attempts=attempts, retry_pass=pass_key,
                )
                processed += tried
                saved += added
                if not error and not deferred:
                    reason = 'Карточка сохранена' if added else 'Пост обработан без создания новой карточки'
                    post_retries.finish_retry(channel, post_id, 'resolved', 'save', reason, attempts,
                                              pass_key=pass_key)
            except Exception as exc:
                logging.error(f'Повторный разбор {url}: {type(exc).__name__}')
                post_retries.finish_retry(channel, post_id, 'pending', 'retry',
                                          f'Ошибка повторного разбора: {type(exc).__name__}', attempts,
                                          pass_key=pass_key)
    return processed, saved, True


async def _send_retry_summary(rows, pass_reference, *, manual):
    resolved = sum(row['status'] == 'resolved' for row in rows)
    pending = sum(row['status'] == 'pending' for row in rows)
    new_reviews = [row for row in rows if row['status'] == 'review' and row['review_notified_at'] is None]
    should_send = bool(manual or resolved or pending or new_reviews)
    visible = []
    text = f'Повторный разбор постов\nЗавершено сегодня: {resolved}. Ждут автоповтора: {pending}.'
    if new_reviews:
        text += f'\nНовые посты, которые не удалось разобрать: {len(new_reviews)}.'
        for row in new_reviews[:20]:
            link = f"\nhttps://t.me/{row['channel']}/{row['post_id']} — {row['reason']}"
            if len(text) + len(link) > 3800:
                break
            text += link
            visible.append(row)
        if len(visible) != len(new_reviews):
            text += '\nПодробности по всем постам есть в диагностике.'
    if not should_send and not pass_reference:
        return
    # A crash or uncertain delivery cannot cause a second copy of this summary.
    # The report has already been saved to diagnostics before claiming delivery.
    stamp = post_retries.claim_summary(visible, pass_reference)
    if should_send and await notify(text) is False:
        post_retries.release_summary(visible, pass_reference, stamp)


async def _replay_and_report(settings, deadline, *, manual, pass_reference=None):
    with ParserPass('post-retries') as lock:
        if not lock.locked:
            logging.info('Другой запуск уже повторно разбирает посты')
            return 0, 0
        pass_key = None
        if pass_reference:
            scope, started_at = pass_reference
            pass_key = f'{scope}:{started_at.astimezone(timezone.utc).isoformat()}'
        processed, saved, complete = await _retry_posts(settings, deadline, include_review=manual,
                                                        pass_key=pass_key)
        rows = post_retries.diagnostic_report()
        post_retries.print_diagnostics(rows, publish=True)
        if complete:
            await _send_retry_summary(rows, pass_reference, manual=manual)
        return processed, saved


async def _retry_settings(channels=None):
    channels = load_channels() if channels is None else channels
    if not channels:
        raise RuntimeError('Список каналов не загружен')
    return channels, load_keywords(), await load_categories_safe(), load_stop_tags()


async def run_parser():
    manual_replay = os.getenv('REPLAY_DEFERRED') == '1'
    part = (os.getenv('CHANNELS_PART') or '').strip()
    scope = (part or 'all') + ':' + (PARSE_SINCE or '')
    full_run = part in ('', '1/1') and not PARSE_SINCE
    deadline = time.monotonic() + MAX_RUN_MINUTES * 60
    try:
        if manual_replay:
            settings = await _retry_settings()
            processed, saved = await _replay_and_report(settings, deadline, manual=True)
        else:
            with ParserPass(scope) as progress:
                if not progress.locked:
                    logging.info('Другой запуск уже обрабатывает эту часть каналов')
                    return 0, 0
                channels = load_channels()
                if not channels:
                    raise RuntimeError('Список каналов не загружен; проход не изменён')
                outcome = await _run_parser_pass(progress, deadline, channels)
                retry_enabled = full_run or os.getenv('GITHUB_EVENT_NAME') == 'schedule'
                state = post_retries.pass_state(scope) if retry_enabled else None
                if outcome is None:
                    if not state or state['retries_complete']:
                        return 0, 0
                    logging.info('Резервный запуск доделывает повторы и сводку')
                    processed, saved, settings = 0, 0, await _retry_settings(channels)
                else:
                    processed, saved, settings = outcome
                if state and state['channels_complete'] and not state['retries_complete']:
                    retried, restored = await _replay_and_report(
                        settings, deadline, manual=False, pass_reference=(scope, state['started_at']),
                    )
                    processed += retried
                    saved += restored
        if dedup is not None:
            try:
                await dedup.finish_run(notify)
            except Exception as exc:
                await notify(f'⚠️ Итог проверки дублей не отправлен: {type(exc).__name__}')
        return processed, saved
    except Exception as exc:
        if manual_replay:
            await notify(f'⚠️ Повторный разбор остановлен: {type(exc).__name__}')
        else:
            await notify(f'⚠️ Запуск парсера прерван. Ход обхода сохранён: {type(exc).__name__}')
        raise
