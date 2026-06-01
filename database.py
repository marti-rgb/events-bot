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
    c.execute('SELECT 1 FROM events WHERE title=%s AND date=%s', (title, date))
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
        ''', event)
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"save_event error: {e}")
        conn.close()
        return False

ddef log_parse(channel: str, post_id: str, model: str, fallback: bool, success: bool, error: str = None, category_l1_arr: list = [], category_l2_arr: list = []):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO parse_logs (channel, post_id, model, fallback, success, error, category_l1_arr, category_l2_arr)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''', (channel, post_id, model, fallback, success, error,
              json.dumps(category_l1_arr, ensure_ascii=False),
              json.dumps(category_l2_arr, ensure_ascii=False)))
        conn.commit()
    except Exception as e:
        logging.error(f"log_parse error: {e}")
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
