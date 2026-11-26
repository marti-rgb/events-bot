# parser.py — v54
# v54 (2026-11-26): Recover publication dates and reprocess the November 26 deferred posts.
# v53 (2026-11-25): Record incomplete events for review and continue to the next channel.
# История версий (старше двух последних) — в versions_history.md
# Переменные в GitHub → Settings → Variables:
#   ANALYZE_THINKING=0  PARSE_PAGES=1  USE_SCREEN=0  CHANNELS_PART=(пусто)
#   PARSE_SINCE=(пусто) — нижняя граница по дате публикации поста, ГГГГ-ММ-ДД
#   DUP_ON=1 — поиск дублей (0 — выключить, парсер работает как раньше)
#   DUP_MODEL=@cf/baai/bge-m3   DUP_FILL_MAX=400 — сколько старых мероприятий получает вектор за прогон
import asyncio
import logging
import time
from contextlib import closing
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import httpx
from bs4 import BeautifulSoup
from analyzer import IncompleteEventError, analyze_post, screen_post
from database import log_parse, start_parse_session, finish_parse_session, ParserPass
from database import is_post_processed, mark_post_processed, save_event, log_rejected_post
from database import load_categories_db, save_categories_db
from database import get_conn
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

# The exact posts in the completed diagnostic run, including dates that remain unresolved.
DEFERRED_BATCH = {
    'artsvao_team': ('1914',),
    'communitymoscow': ('9796', '9801', '9802'),
    'freeskidos': ('35270',),
    'kids_msk_afisha': ('4300',),
    'kudago': ('45764',),
    'mammkino': ('613',),
    'moscowculturelife': ('7744',),
    'moscowimprovclub': ('5822',),
    'mosgul': ('26588',),
    'oteatre': ('11192',),
    'terragames': ('8348', '8353'),
    'zaryadye_official': ('20270',),
}
REPLAY_STAGE = 'replay_deferred_20261126'

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

async def parse_channel(client: httpx.AsyncClient, channel_config: dict, filter_keywords: list = [], categories: dict = {}, stop_tags: list = [], *, deferred_urls=None, replay_post=None):
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
            if is_post_processed(channel, int(msg_id)):
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

        if USE_SCREEN:
            is_event, screen_model = await screen_post(post['text'])
            log_parse(
                channel=channel,
                post_id=str(msg_id),
                model=screen_model or 'none',
                fallback=bool(screen_model and not screen_model.startswith('zai/')),
                success=screen_model is not None,
                stage='screen', qualified=False if screen_model is not None and not is_event else None, metrics_version=1,
            )
            if screen_model is None:
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
            result = await analyze_post(post['text'], post.get('post_date', ''), categories, post.get('links'))
        except IncompleteEventError:
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
            # Save the review link before preventing further automatic analysis of this post.
            mark_post_processed(channel, int(msg_id))
            deferred += 1
            logging.warning(f"Отложено на разбор: нет названия или даты. {post_url}")
            if deferred_urls is not None:
                deferred_urls.append(post_url)
            elif replay_post is None:
                await notify(f"⚠️ Пост отложен на разбор: нет названия или даты события.\n{post_url}")
            await asyncio.sleep(1.5)
            continue
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


async def _run_parser_pass(progress, deferred_urls):
    logging.info("Начинаем парсинг через t.me/s/...")
    
    CHANNELS = load_channels()
    if not CHANNELS:
        raise RuntimeError('Список каналов не загружен; проход не изменён')

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
        logging.info('Завершённый проход сегодня уже был; резервный запуск пропущен')
        return 0, 0
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
    
    run_started = time.monotonic()
    async with httpx.AsyncClient() as client:
        for idx in range(progress.next_index, len(CHANNELS)):
            channel_config = CHANNELS[idx]
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
            fetched, processed, saved, skipped, error, deferred = await parse_channel(
                client, channel_config, FILTER_KEYWORDS, CATEGORIES, STOP_TAGS, deferred_urls=deferred_urls,
            )
            total_fetched += fetched
            total_processed += processed
            total_saved += saved
            total_skipped += skipped
            total_error += error + deferred
            if error:
                await notify(f"⚠️ @{channel}: есть незавершённые посты. Следующий запуск продолжит с этого канала.")
                break
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

    # patch 2 (v50): one summary about duplicates — in the log always, to the admin only if there is
    # something to say (new groups) or something went wrong.
    if dedup is not None:
        try:
            await dedup.finish_run(notify)
        except Exception as e:
            logging.error(f"Итог по дублям не отправлен: {e}")

    return total_processed, total_saved


async def replay_deferred_batch():
    channels = {config['channel']: config for config in load_channels()}
    if not channels:
        raise RuntimeError('Список каналов для повторного разбора не загружен')

    batch = [(channel, post_id, f'https://t.me/{channel}/{post_id}')
             for channel, post_ids in DEFERRED_BATCH.items() for post_id in post_ids]
    with closing(get_conn()) as conn:
        conn.set_session(readonly=True)
        with conn.cursor() as cursor:
            cursor.execute('''
                SELECT 'https://t.me/' || channel || '/' || post_id AS url
                FROM public.parse_logs WHERE stage = %s AND success IS TRUE
                UNION
                SELECT source_url FROM public.events WHERE source_url = ANY(%s)
            ''', (REPLAY_STAGE, [url for _, _, url in batch]))
            handled = {row['url'] for row in cursor.fetchall()}

    categories = await load_categories_safe()
    keywords = load_keywords()
    stop_tags = load_stop_tags()
    processed = 0
    saved = 0
    skipped = 0
    pending = []
    async with httpx.AsyncClient() as client:
        for channel, post_id, url in batch:
            if url in handled:
                skipped += 1
                continue
            if channel not in channels:
                pending.append(f'{url} — канала нет в списке парсера')
                continue
            try:
                page = await _fetch_one_page(client, channel, f'https://t.me/s/{channel}?before={int(post_id) + 1}')
                post = next((candidate for candidate in page if candidate['id'] == post_id), None)
                if post is None:
                    pending.append(f'{url} — пост не найден в Telegram')
                    continue

                _, tried, added, _, error, deferred = await parse_channel(
                    client, channels[channel], keywords, categories, stop_tags, replay_post=post,
                )
                processed += tried
                saved += added
                if deferred or error:
                    reason = 'нет подтверждённой даты или названия' if deferred else 'разбор не завершился'
                    pending.append(f'{url} — {reason}')
                    continue

                if not added:
                    # Saved events are already checkpointed by their unique source URL.
                    log_parse(channel=channel, post_id=post_id, model=None, fallback=False,
                              success=True, stage=REPLAY_STAGE)
                    skipped += 1
            except Exception as exc:
                logging.error(f'Повторный разбор {url}: {type(exc).__name__}')
                pending.append(f'{url} — ошибка {type(exc).__name__}')

    summary = (f'Повторный разбор отложенных постов от 26 ноября\n'
               f'Всего: {len(batch)}. Сохранено: {saved}. Уже в базе или отсеяно: {skipped}. '
               f'Осталось: {len(pending)}.')
    if pending:
        summary += '\n' + '\n'.join(pending)
    logging.info(summary)
    await notify(summary)

    if dedup is not None:
        try:
            await dedup.finish_run(notify)
        except Exception as exc:
            await notify(f'⚠️ Итог проверки дублей после повторного разбора не отправлен: {type(exc).__name__}')
    return processed, saved


async def run_parser():
    replay = os.getenv('REPLAY_DEFERRED') == '1'
    scope = (os.getenv('CHANNELS_PART') or 'all') + ':' + (PARSE_SINCE or '')
    deferred_urls = []
    try:
        with ParserPass(REPLAY_STAGE if replay else scope) as progress:
            if not progress.locked:
                logging.info('Другой запуск уже обрабатывает эту часть каналов')
                return 0, 0
            if replay:
                return await replay_deferred_batch()
            return await _run_parser_pass(progress, deferred_urls)
    except Exception as exc:
        if replay:
            await notify(f'⚠️ Повторный разбор остановлен: {type(exc).__name__}')
            raise
        await notify('⚠️ Парсер остановлен; место продолжения сохранено: ' + str(exc)[:250])
        raise
    finally:
        if deferred_urls:
            links = '\n'.join(deferred_urls[:30])
            extra = '\nОстальные ссылки в журнале запуска.' if len(deferred_urls) > 30 else ''
            await notify(f'⚠️ Посты отложены на разбор: {len(deferred_urls)}. '
                         f'Не удалось определить название или дату события.\n{links}{extra}')
