import sqlite3
import os
from datetime import datetime

DB_PATH = os.getenv('DB_PATH', 'events.db')

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    
    # Таблица мероприятий
    c.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            date TEXT,
            time TEXT,
            is_free INTEGER,
            for_children INTEGER,
            format TEXT,
            category TEXT,
            description TEXT,
            source_url TEXT UNIQUE,
            channel TEXT,
            city TEXT DEFAULT 'Москва',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Таблица обработанных постов (чтобы не обрабатывать повторно)
    c.execute('''
        CREATE TABLE IF NOT EXISTS processed_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT,
            message_id INTEGER,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(channel, message_id)
        )
    ''')
    
    conn.commit()
    conn.close()

def is_post_processed(channel: str, message_id: int) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT 1 FROM processed_posts WHERE channel=? AND message_id=?', (channel, message_id))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_post_processed(channel: str, message_id: int):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('INSERT INTO processed_posts (channel, message_id) VALUES (?, ?)', (channel, message_id))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

def event_exists(title: str, date: str) -> bool:
    """Проверка дубликата по названию и дате"""
    if not title or not date:
        return False
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT 1 FROM events WHERE title=? AND date=?', (title, date))
    result = c.fetchone()
    conn.close()
    return result is not None

def save_event(event: dict) -> bool:
    """Сохранить мероприятие. Вернёт False если дубликат."""
    if event_exists(event.get('title', ''), event.get('date', '')):
        return False
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO events (title, date, time, is_free, for_children, format, category, description, source_url, channel, city)
            VALUES (:title, :date, :time, :is_free, :for_children, :format, :category, :description, :source_url, :channel, :city)
        ''', event)
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False

def get_all_events():
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM events ORDER BY date, time')
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]
