import os
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
        c.execute('''
            INSERT INTO events (title, date, time, is_free, for_children, format, category, description, source_url, channel, city)
            VALUES (%(title)s, %(date)s, %(time)s, %(is_free)s, %(for_children)s, %(format)s, %(category)s, %(description)s, %(source_url)s, %(channel)s, %(city)s)
            ON CONFLICT (source_url) DO NOTHING
        ''', event)
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"save_event error: {e}")
        conn.close()
        return False
