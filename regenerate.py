import os
import json
import logging
import asyncio
import psycopg2
from analyzer import analyze_post
from sheets_config import load_categories

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# DB_URL = os.getenv('DATABASE_URL')

def get_connection():
    return psycopg2.connect(
        host="aws-1-eu-central-1.pooler.supabase.com",
        port=5432,
        dbname="postgres",
        user="postgres.phkwivzwzcowgnpnlxre",
        password=os.environ.get("DB_PASSWORD"),
        connect_timeout=10
    )

def get_events_to_regenerate(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, description, date, source_url
            FROM events
            WHERE (model IS NULL OR model = 'groq/llama-3.1-8b-instant')
            AND date >= CURRENT_DATE
            ORDER BY date ASC
        """)
        return cur.fetchall()

def update_event(conn, event_id, result):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE events SET
                category_l1_arr = %s,
                category_l2_arr = %s,
                city_parsed = %s,
                is_free = %s,
                for_children = %s,
                format = %s,
                location = %s,
                address = %s,
                model = %s
            WHERE id = %s
        """, (
            json.dumps(result.get('category_l1_arr', []), ensure_ascii=False),
            json.dumps(result.get('category_l2_arr', []), ensure_ascii=False),
            json.dumps(result.get('city_parsed', []), ensure_ascii=False),
            result.get('is_free'),
            result.get('for_children'),
            result.get('format'),
            result.get('location'),
            result.get('address'),
            result.get('model'),
            event_id
        ))
        conn.commit()

async def main():
    categories = load_categories()
    conn = get_connection()
    events = get_events_to_regenerate(conn)
    logging.info(f"Записей для перегенерации: {len(events)}")

    ok = 0
    fail = 0
    for row in events:
        event_id, title, description, date, source_url = row
        text = f"{title}\n{description or ''}"
        if not text.strip():
            continue
        result = await analyze_post(text, str(date) if date else '', categories)
        if result:
            update_event(conn, event_id, result)
            ok += 1
            logging.info(f"✅ [{ok}] id={event_id} | {title}")
        else:
            fail += 1
            logging.warning(f"❌ id={event_id} | {title}")
        await asyncio.sleep(1.5)

    conn.close()
    logging.info(f"Готово. Обновлено: {ok}, ошибок: {fail}")

if __name__ == '__main__':
    asyncio.run(main())
