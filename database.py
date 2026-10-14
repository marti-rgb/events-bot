# database.py — v3 (имя файла не меняется: его импортирует parser.py)
# v3 — categories_catalog: load_categories_db() reads the copy of the sheet «Категории», save_categories_db() overwrites it after every successful read of the sheet (patch 1, 14.10.2026). Updated 14.10.2026
# v2 — event_exists и проверка похожего описания сравнивают только с видимыми мероприятиями (опубликованы или в очереди queue); черновики, модерация, удалённые, отклонённые и дубликаты больше не прячут мероприятие из канала. Обновлено 03.10.2026
import os
import json
import logging
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv('DATABASE_URL')

def get_conn():
    return psycopg2.connect(
        host="aws-1-eu-central-1.pooler.supabase.com",
        port=5432,
        dbname="postgres",
        user="postgres.phkwivzwzcowgnpnlxre",
        password=os.environ.get("DB_PASSWORD"),
        cursor_factory=RealDictCursor,
        connect_timeout=10
    )
def init_db():
    conn = get_conn()
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            date TEXT,
            time TEXT,
            is_free BOOLEAN,
            for_children BOOLEAN,
            format TEXT,
            category TEXT,
            description TEXT,
            source_url TEXT UNIQUE,
            channel TEXT,
            city TEXT DEFAULT 'Москва',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS processed_posts (
            id SERIAL PRIMARY KEY,
            channel TEXT,
            message_id INTEGER,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(channel, message_id)
        )
    ''')
    
    conn.commit()
    conn.close()
    logging.info("БД инициализирована")

def is_post_processed(channel: str, message_id: int) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT 1 FROM processed_posts WHERE channel=%s AND message_id=%s', (channel, message_id))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_post_processed(channel: str, message_id: int):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('INSERT INTO processed_posts (channel, message_id) VALUES (%s, %s) ON CONFLICT DO NOTHING', (channel, message_id))
        conn.commit()
    except Exception as e:
        logging.error(f"mark_post_processed error: {e}")
    finally:
        conn.close()

def event_exists(title: str, date: str) -> bool:
    if not title or not date:
        return False
    conn = get_conn()
    c = conn.cursor()
    # patch 1: only visible events (published or queued) block saving; drafts/moderation/deleted/rejected/duplicates don't
    c.execute('''
        SELECT 1 FROM events WHERE title=%s AND date=%s
        AND (COALESCE(is_ignored, false) = false OR ignored_reason = 'queue')
    ''', (title, date))
    result = c.fetchone()
    conn.close()
    return result is not None

def save_event(event: dict) -> bool:
    if event_exists(event.get('title', ''), event.get('date', '')):
        return False
    conn = get_conn()
    c = conn.cursor()
    try:
        logging.info(f"categories before save: {event.get('category_l1_arr')} / {event.get('category_l2_arr')}")
        logging.info(f"saving arr: {event.get('category_l1_arr')} / {event.get('category_l2_arr')}")
        event['category_l1_arr'] = json.dumps(event.get('category_l1_arr', []), ensure_ascii=False)
        event['category_l2_arr'] = json.dumps(event.get('category_l2_arr', []), ensure_ascii=False)
        event['city_parsed'] = json.dumps(event.get('city_parsed', []), ensure_ascii=False)
        c.execute('''
            INSERT INTO events (title, date, time, is_free, for_children, format, category_l1_arr, category_l2_arr, description, location, address, price, source_url, channel, city, model, city_parsed)
            VALUES (%(title)s, %(date)s, %(time)s, %(is_free)s, %(for_children)s, %(format)s, %(category_l1_arr)s, %(category_l2_arr)s, %(description)s, %(location)s, %(address)s, %(price)s, %(source_url)s, %(channel)s, %(city)s, %(model)s, %(city_parsed)s)
            ON CONFLICT (source_url) DO NOTHING
            RETURNING id
        ''', event)
        row = c.fetchone()
        if row and event.get('description'):
            new_id = row['id']
            c.execute('''
                SELECT id FROM events 
                WHERE date = %s 
                AND description IS NOT NULL
                AND similarity(description, %s) > 0.95
                AND id != %s
                AND (COALESCE(is_ignored, false) = false OR ignored_reason = 'queue')
                LIMIT 1
            ''', (event.get('date'), event.get('description'), new_id))
            duplicate = c.fetchone()
            if duplicate:
                c.execute('''
                    UPDATE events SET is_ignored = true, ignored_reason = 'duplicate'
                    WHERE id = %s
                ''', (new_id,))
                logging.info(f"Дубликат помечен: id={new_id}, оригинал id={duplicate['id']}")
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"save_event error: {e}")
        conn.close()
        return False

def log_parse(channel: str, post_id: str, model: str, fallback: bool, success: bool, error: str = None, category_l1_arr: list = [], category_l2_arr: list = [], stage: str = 'analyze'):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO parse_logs (channel, post_id, model, fallback, success, error, category_l1_arr, category_l2_arr, stage)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (channel, post_id, model, fallback, success, error,
              json.dumps(category_l1_arr, ensure_ascii=False),
              json.dumps(category_l2_arr, ensure_ascii=False),
              stage))
        conn.commit()
    except Exception as e:
        logging.error(f"log_parse error: {e}")
    finally:
        conn.close()

# ===== patch 1 (v3): copy of the sheet «Категории» in the database =====
# The parser reads the sheet first; on success it overwrites categories_catalog, so the copy is always fresh.
# If the sheet did not answer, the parser reads this copy. The bots' catalog (get_categories_l1) is limited to it.
CATALOG_MIN_SECTIONS = 5  # a sheet read with fewer sections is treated as broken and does not overwrite the copy

def load_categories_db() -> dict:
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"load_categories_db connect error: {e}")
        return {}
    c = conn.cursor()
    try:
        c.execute('SELECT category_l1, category_l2 FROM categories_catalog ORDER BY id')
        cats = {}
        for row in c.fetchall():
            l1 = (row['category_l1'] or '').strip()
            l2 = (row['category_l2'] or '').strip()
            if not l1:
                continue
            cats.setdefault(l1, [])
            if l2 and l2 not in cats[l1]:
                cats[l1].append(l2)
        logging.info(f"Справочник из базы: разделов {len(cats)}")
        return cats
    except Exception as e:
        logging.error(f"load_categories_db error: {e}")
        return {}
    finally:
        conn.close()

def save_categories_db(categories: dict):
    """Returns True (saved), False (database error) or None (not saved: the sheet looks broken)."""
    if not categories or len(categories) < CATALOG_MIN_SECTIONS:
        logging.warning(f"save_categories_db: в листе всего {len(categories or {})} разделов — копию в базе не перезаписываю")
        return None
    rows = []
    for l1, l2_list in categories.items():
        for l2 in (l2_list or ['']):
            rows.append((len(rows) + 1, l1, l2 or ''))
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"save_categories_db connect error: {e}")
        return False
    c = conn.cursor()
    try:
        c.execute('LOCK TABLE categories_catalog IN EXCLUSIVE MODE')  # parallel parser parts wait for each other
        c.execute('DELETE FROM categories_catalog')
        c.executemany(
            'INSERT INTO categories_catalog (id, category_l1, category_l2) VALUES (%s, %s, %s)',
            rows)
        conn.commit()
        logging.info(f"Копия справочника в базе обновлена: разделов {len(categories)}, строк {len(rows)}")
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"save_categories_db error: {e}")
        return False
    finally:
        conn.close()

def log_rejected_post(channel: str, post_id: str, text: str):
    """Сохраняет текст поста, который нейросеть сочла НЕ событием —
    для последующего поиска новых стоп-слов частотным анализом.
    Никак не влияет на сохранение событий, только на этот побочный лог."""
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO rejected_posts (channel, post_id, text)
            VALUES (%s, %s, %s)
        ''', (channel, post_id, (text or '')[:2000]))
        conn.commit()
    except Exception as e:
        logging.error(f"log_rejected_post error: {e}")
    finally:
        conn.close()

def start_parse_session(github_run_id: str = None, github_run_url: str = None) -> int:
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO parse_sessions (github_run_id, github_run_url)
            VALUES (%s, %s) RETURNING id
        ''', (github_run_id, github_run_url))
        session_id = c.fetchone()['id']
        conn.commit()
        return session_id
    except Exception as e:
        logging.error(f"start_parse_session error: {e}")
        return None
    finally:
        conn.close()

def finish_parse_session(session_id: int, stats: dict):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('SELECT COUNT(*) as cnt FROM events', )
        total = c.fetchone()['cnt']
        c.execute('''
            UPDATE parse_sessions SET
                session_end = now(),
                posts_fetched = %s,
                posts_processed = %s,
                posts_saved = %s,
                posts_skipped = %s,
                posts_error = %s,
                channels_processed = %s,
                total_events_in_db = %s
            WHERE id = %s
        ''', (
            stats.get('fetched', 0),
            stats.get('processed', 0),
            stats.get('saved', 0),
            stats.get('skipped', 0),
            stats.get('error', 0),
            stats.get('channels', 0),
            total,
            session_id
        ))
        conn.commit()
    except Exception as e:
        logging.error(f"finish_parse_session error: {e}")
    finally:
        conn.close()

def has_session_today() -> bool:
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('''
            SELECT COUNT(*) as cnt FROM parse_sessions
            WHERE session_start::date = CURRENT_DATE
        ''')
        return c.fetchone()['cnt'] > 0
    except Exception as e:
        logging.error(f"has_session_today error: {e}")
        return False
    finally:
        conn.close()
