# database.py — v6 (имя файла не меняется: его импортирует parser.py)
# v6 — task 56, old events: list/count of future events never checked for duplicates, bulk vector save,
#      find_dup_candidates(only_checked=…) so every pair is looked at once, a pair already sharing a group is
#      never offered again, a pair is not offered if it would put two different times into one waiting group,
#      past waiting groups are closed (status 'expired'), setting dup_old_ai_budget, get_setting/set_setting.
#      No schema change; the dup_old_ai_budget row is block 1 of migration_20261022_dup_old.sql. Updated 22.10.2026
# v5 — task 56 (semantic duplicates): save_event() returns the new event id instead of True/False, plus the
#      database side of dedup.py — vectors (event_vectors), candidate search inside the same day + city,
#      groups for manual review (dup_groups / dup_group_items) and the «кого оставить» rule.
#      Needs migration_20261021_dup.sql. Updated 21.10.2026
#
# История версий (старше двух последних) — в versions_history.md
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

def save_event(event: dict):
    """v5: returns the id of the saved event, or None. The parser keeps using it as a yes/no check."""
    if event_exists(event.get('title', ''), event.get('date', '')):
        return None
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
            ON CONFLICT (source_url) WHERE ignored_reason IS DISTINCT FROM 'manual' DO NOTHING
            RETURNING id
        ''', event)
        row = c.fetchone()
        new_id = row['id'] if row else None
        if row and event.get('description'):
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
                new_id = None  # already hidden by the old 0.95 check — nothing for dedup.py to do
        conn.commit()
        conn.close()
        return new_id
    except Exception as e:
        logging.error(f"save_event error: {e}")
        conn.close()
        return None

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


# ===== v5: semantic duplicates (task 56). The brain lives in dedup.py, the SQL lives here. =====
# Tables and settings come from migration_20261021_dup.sql:
#   event_vectors (event_id, vec, model, built_at)
#   dup_groups (id, day, city, keep_id, status, decided_at, created_at)
#   dup_group_items (group_id, event_id, score, by_ai, reason, hidden)
#   bot_settings: dup_hi, dup_lo, dup_auto, dup_ai_budget

DUP_DEFAULTS = {'dup_hi': 0.93, 'dup_lo': 0.80, 'dup_auto': 'off', 'dup_ai_budget': 50,
                'dup_old_ai_budget': 100}

# The same «today» the add-bot and cleanup_event_vectors() use: events.date is text 'YYYY-MM-DD', Moscow time.
_TODAY_MSK = "to_char(now() AT TIME ZONE 'Europe/Moscow', 'YYYY-MM-DD')"


def get_dup_settings() -> dict:
    """Thresholds and switches, read once per run. Any missing or broken value falls back to the default."""
    out = {'hi': DUP_DEFAULTS['dup_hi'], 'lo': DUP_DEFAULTS['dup_lo'],
           'auto': False, 'ai_budget': DUP_DEFAULTS['dup_ai_budget'],
           'old_ai_budget': DUP_DEFAULTS['dup_old_ai_budget']}
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"get_dup_settings connect error: {e}")
        return out
    c = conn.cursor()
    try:
        c.execute("SELECT key, value FROM bot_settings WHERE key LIKE 'dup\\_%'")
        raw = {row['key']: (row['value'] or '').strip() for row in c.fetchall()}
        try:
            out['hi'] = float(raw.get('dup_hi', DUP_DEFAULTS['dup_hi']))
        except ValueError:
            pass
        try:
            out['lo'] = float(raw.get('dup_lo', DUP_DEFAULTS['dup_lo']))
        except ValueError:
            pass
        try:
            out['ai_budget'] = max(0, int(raw.get('dup_ai_budget', DUP_DEFAULTS['dup_ai_budget'])))
        except ValueError:
            pass
        try:
            out['old_ai_budget'] = max(0, int(raw.get('dup_old_ai_budget', DUP_DEFAULTS['dup_old_ai_budget'])))
        except ValueError:
            pass
        out['auto'] = raw.get('dup_auto', 'off').lower() in ('1', 'on', 'true', 'yes')
        if out['lo'] > out['hi']:
            out['lo'] = out['hi']
        return out
    except Exception as e:
        logging.error(f"get_dup_settings error: {e}")
        return out
    finally:
        conn.close()


def _vec_literal(vec) -> str:
    return '[' + ','.join(f"{float(x):.6f}" for x in vec) + ']'


def save_event_vector(event_id: int, vec, model: str) -> bool:
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"save_event_vector connect error: {e}")
        return False
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO event_vectors (event_id, vec, model)
            VALUES (%s, %s::vector, %s)
            ON CONFLICT (event_id) DO UPDATE SET vec = excluded.vec, model = excluded.model, built_at = now()
        ''', (event_id, _vec_literal(vec), model))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"save_event_vector error (event {event_id}): {e}")
        return False
    finally:
        conn.close()


def save_event_vectors_bulk(rows: list, model: str) -> int:
    """v6: many vectors in one connection — the old-events check builds hundreds at once.
    rows = [(event_id, vec), …]. Returns how many were saved (0 on any failure: the caller just skips them)."""
    if not rows:
        return 0
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"save_event_vectors_bulk connect error: {e}")
        return 0
    c = conn.cursor()
    try:
        c.executemany('''
            INSERT INTO event_vectors (event_id, vec, model)
            VALUES (%s, %s::vector, %s)
            ON CONFLICT (event_id) DO UPDATE SET vec = excluded.vec, model = excluded.model, built_at = now()
        ''', [(eid, _vec_literal(vec), model) for eid, vec in rows])
        conn.commit()
        return len(rows)
    except Exception as e:
        conn.rollback()
        logging.error(f"save_event_vectors_bulk error: {e}")
        return 0
    finally:
        conn.close()


def event_city(event_id: int):
    """City the way search understands it: what the AI found in the post, otherwise the channel's city."""
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"event_city connect error: {e}")
        return None
    c = conn.cursor()
    try:
        c.execute("""
            SELECT COALESCE(NULLIF(city_parsed->>0, ''), NULLIF(city, '')) AS city
            FROM events WHERE id = %s
        """, (event_id,))
        row = c.fetchone()
        return row['city'] if row else None
    except Exception as e:
        logging.error(f"event_city error: {e}")
        return None
    finally:
        conn.close()


def find_events_without_vector(day: str, city, limit: int) -> list:
    """Events of the same day and city that have no vector yet — the backfill of what was already in the base."""
    if limit <= 0:
        return []
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"find_events_without_vector connect error: {e}")
        return []
    c = conn.cursor()
    try:
        c.execute("""
            SELECT e.id, e.title, e.location, e.address, e.description
            FROM events e
            LEFT JOIN event_vectors v ON v.event_id = e.id
            WHERE v.event_id IS NULL
              AND e.date = %s
              AND COALESCE(NULLIF(e.city_parsed->>0, ''), NULLIF(e.city, '')) IS NOT DISTINCT FROM %s
              AND (COALESCE(e.is_ignored, false) = false OR e.ignored_reason = 'queue')
            ORDER BY e.id DESC
            LIMIT %s
        """, (day, city, limit))
        return [dict(row) for row in c.fetchall()]
    except Exception as e:
        logging.error(f"find_events_without_vector error: {e}")
        return []
    finally:
        conn.close()


def find_dup_candidates(event_id: int, limit: int, min_score: float, only_checked: bool = False) -> list:
    """Nearest events by meaning inside the same day and city.
    Skipped: other time (unless one of the two has none), rows of the same post, pairs that already share a
    group (waiting, decided, «не дубли» or taken out by the admin — v6: any group, so no pair is judged twice),
    and everything hidden from search.
    only_checked (v6, old-events check): only neighbours already checked for duplicates. Two unchecked events
    meet once — when the second of them is checked, the first one is already marked."""
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"find_dup_candidates connect error: {e}")
        return []
    c = conn.cursor()
    try:
        c.execute("""
            WITH me AS (
                SELECT e.id,
                       e.date,
                       COALESCE(NULLIF(e.city_parsed->>0, ''), NULLIF(e.city, '')) AS city,
                       NULLIF(e.time, '') AS tm,
                       split_part(COALESCE(e.source_url, ''), '#', 1) AS post_url,
                       v.vec AS vec
                FROM events e
                JOIN event_vectors v ON v.event_id = e.id
                WHERE e.id = %s
            )
            SELECT e.id, e.title, e.date, e.time, e.location, e.address, e.price, e.description,
                   (1 - (v.vec <=> me.vec)) AS score
            FROM events e
            JOIN event_vectors v ON v.event_id = e.id
            CROSS JOIN me
            WHERE e.id <> me.id
              AND e.date = me.date
              AND COALESCE(NULLIF(e.city_parsed->>0, ''), NULLIF(e.city, '')) IS NOT DISTINCT FROM me.city
              AND (COALESCE(e.is_ignored, false) = false OR e.ignored_reason = 'queue')
              AND (NULLIF(e.time, '') IS NULL OR me.tm IS NULL OR NULLIF(e.time, '') = me.tm)
              AND split_part(COALESCE(e.source_url, ''), '#', 1) <> me.post_url
              AND NOT EXISTS (
                    SELECT 1
                    FROM dup_group_items a
                    JOIN dup_group_items b ON b.group_id = a.group_id
                    WHERE a.event_id = me.id AND b.event_id = e.id
              )
              -- v6: «другое время — не дубль» must hold for the whole group, not only for the pair. A pair joins
              -- the waiting group of either side, so neither side may bring a time that differs from a time
              -- already in the other side's group (mafia 19:00 and 21:00 next to a post without time).
              AND NOT EXISTS (
                    SELECT 1
                    FROM dup_group_items gi
                    JOIN dup_groups gg ON gg.id = gi.group_id AND gg.status = 'wait'
                    JOIN dup_group_items gj ON gj.group_id = gi.group_id AND COALESCE(gj.reason, '') <> 'excluded'
                    JOIN events ej ON ej.id = gj.event_id
                    WHERE gi.event_id = e.id
                      AND me.tm IS NOT NULL
                      AND NULLIF(ej.time, '') IS NOT NULL AND NULLIF(ej.time, '') <> me.tm
              )
              AND NOT EXISTS (
                    SELECT 1
                    FROM dup_group_items mi
                    JOIN dup_groups mg ON mg.id = mi.group_id AND mg.status = 'wait'
                    JOIN dup_group_items mj ON mj.group_id = mi.group_id AND COALESCE(mj.reason, '') <> 'excluded'
                    JOIN events ek ON ek.id = mj.event_id
                    WHERE mi.event_id = me.id
                      AND NULLIF(e.time, '') IS NOT NULL
                      AND NULLIF(ek.time, '') IS NOT NULL AND NULLIF(ek.time, '') <> NULLIF(e.time, '')
              )
              AND (%s = false OR e.dup_checked_at IS NOT NULL)
              AND (1 - (v.vec <=> me.vec)) >= %s
            ORDER BY v.vec <=> me.vec
            LIMIT %s
        """, (event_id, bool(only_checked), min_score, limit))
        return [dict(row) for row in c.fetchall()]
    except Exception as e:
        logging.error(f"find_dup_candidates error (event {event_id}): {e}")
        return []
    finally:
        conn.close()


# Which one stays, in the order agreed with the user: organizer → price → address → photo → registration
# link → longer description → added later. The first line is also the human-readable reason.
_KEEP_ORDER = """
    (added_by_user IS NOT NULL) DESC,
    (COALESCE(NULLIF(price, ''), '') <> '') DESC,
    (COALESCE(NULLIF(address, ''), '') <> '') DESC,
    (COALESCE(NULLIF(photo_id, ''), '') <> '' OR media_message_id IS NOT NULL) DESC,
    (COALESCE(NULLIF(reg_link, ''), '') <> '') DESC,
    length(COALESCE(description, '')) DESC,
    created_at DESC,
    id DESC
"""


def _recompute_keep(c, group_id: int):
    """Picks the survivor of the group and writes a short reason into dup_groups.keep_id / items."""
    c.execute(f"""
        SELECT id,
               (added_by_user IS NOT NULL) AS is_org,
               (COALESCE(NULLIF(price, ''), '') <> '') AS has_price,
               (COALESCE(NULLIF(address, ''), '') <> '') AS has_addr,
               (COALESCE(NULLIF(photo_id, ''), '') <> '' OR media_message_id IS NOT NULL) AS has_photo,
               (COALESCE(NULLIF(reg_link, ''), '') <> '') AS has_link
        FROM events
        WHERE id IN (SELECT event_id FROM dup_group_items
                     WHERE group_id = %s AND COALESCE(reason, '') <> 'excluded')
        ORDER BY {_KEEP_ORDER}
        LIMIT 1
    """, (group_id,))
    row = c.fetchone()
    if not row:
        return None
    if row['is_org']:
        why = 'от организатора'
    elif row['has_price']:
        why = 'указана цена'
    elif row['has_addr']:
        why = 'указан адрес'
    elif row['has_photo']:
        why = 'есть фото'
    elif row['has_link']:
        why = 'есть ссылка на регистрацию'
    else:
        why = 'добавлено позже'
    c.execute('UPDATE dup_groups SET keep_id = %s WHERE id = %s', (row['id'], group_id))
    c.execute("""
        UPDATE dup_group_items SET reason = %s
        WHERE group_id = %s AND event_id = %s AND COALESCE(reason, '') <> 'excluded'
    """, (f'оставляем: {why}', group_id, row['id']))
    return row['id']


def create_or_join_group(day: str, city, new_id: int, other_id: int, score: float, by_ai: bool, reason: str):
    """Returns (joined, group_id). If one of the two already waits in a group, the pair joins it
    instead of creating a second group about the same event."""
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"create_or_join_group connect error: {e}")
        return False, None
    c = conn.cursor()
    try:
        c.execute("""
            SELECT g.id FROM dup_groups g
            JOIN dup_group_items i ON i.group_id = g.id
            WHERE g.status = 'wait' AND i.event_id IN (%s, %s)
            ORDER BY g.id LIMIT 1
        """, (new_id, other_id))
        row = c.fetchone()
        joined = bool(row)
        if joined:
            group_id = row['id']
        else:
            c.execute('INSERT INTO dup_groups (day, city) VALUES (%s, %s) RETURNING id', (day, city))
            group_id = c.fetchone()['id']
        c.execute("""
            INSERT INTO dup_group_items (group_id, event_id, score, by_ai, reason)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (group_id, event_id) DO NOTHING
        """, (group_id, other_id, None, False, ''))
        c.execute("""
            INSERT INTO dup_group_items (group_id, event_id, score, by_ai, reason)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (group_id, event_id) DO UPDATE
                SET score = excluded.score, by_ai = excluded.by_ai, reason = excluded.reason
        """, (group_id, new_id, round(float(score), 3), by_ai, (reason or '')[:200]))
        _recompute_keep(c, group_id)
        conn.commit()
        logging.info(f"Дубли: {'дописано в группу' if joined else 'новая группа'} {group_id} "
                     f"({new_id} ↔ {other_id}, похожесть {score:.2f})")
        return joined, group_id
    except Exception as e:
        conn.rollback()
        logging.error(f"create_or_join_group error ({new_id} ↔ {other_id}): {e}")
        return False, None
    finally:
        conn.close()


def hide_losers(group_id: int) -> int:
    """Automatic mode only (bot_settings.dup_auto = on). Hides everyone in the group but the survivor.
    Hidden, not deleted: ignored_reason = 'duplicate' and dup_of points at the survivor."""
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"hide_losers connect error: {e}")
        return 0
    c = conn.cursor()
    try:
        c.execute('SELECT keep_id FROM dup_groups WHERE id = %s', (group_id,))
        row = c.fetchone()
        keep_id = row['keep_id'] if row else None
        if not keep_id:
            return 0
        c.execute("""
            UPDATE events SET is_ignored = true, ignored_reason = 'duplicate', dup_of = %s
            WHERE id IN (SELECT event_id FROM dup_group_items
                         WHERE group_id = %s AND COALESCE(reason, '') <> 'excluded')
              AND id <> %s
              AND (COALESCE(is_ignored, false) = false OR ignored_reason = 'queue')
            RETURNING id
        """, (keep_id, group_id, keep_id))
        hidden = [r['id'] for r in c.fetchall()]
        if hidden:
            c.execute("""
                UPDATE dup_group_items SET hidden = true
                WHERE group_id = %s AND event_id = ANY(%s)
            """, (group_id, hidden))
        c.execute("""
            UPDATE dup_groups SET status = 'auto', decided_at = now() WHERE id = %s
        """, (group_id,))
        conn.commit()
        return len(hidden)
    except Exception as e:
        conn.rollback()
        logging.error(f"hide_losers error (group {group_id}): {e}")
        return 0
    finally:
        conn.close()


def mark_dup_checked(event_id: int):
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"mark_dup_checked connect error: {e}")
        return
    c = conn.cursor()
    try:
        c.execute('UPDATE events SET dup_checked_at = now() WHERE id = %s', (event_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"mark_dup_checked error: {e}")
    finally:
        conn.close()


# ===== v6: old events (task 56) — everything that was in the base before the duplicate check existed =====
# «Old» = a future event, visible in search (or waiting in the 3-day queue), never checked: dup_checked_at is
# empty. The parser marks its new events itself, so after the first full pass only add-bot events and the
# events the parser could not finish (judge budget, Cloudflare down) show up here.
_OLD_WHERE = f"""
    e.dup_checked_at IS NULL
    AND COALESCE(e.date, '') <> ''
    AND e.date >= {_TODAY_MSK}
    AND (COALESCE(e.is_ignored, false) = false OR e.ignored_reason = 'queue')
"""


def list_old_unchecked(limit: int) -> list:
    """Nearest dates first: those leave the base soonest and matter to users right now."""
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"list_old_unchecked connect error: {e}")
        return []
    c = conn.cursor()
    try:
        c.execute(f"""
            SELECT e.id, e.title, e.date, e.time, e.location, e.address, e.price, e.description,
                   COALESCE(NULLIF(e.city_parsed->>0, ''), NULLIF(e.city, '')) AS city,
                   (v.event_id IS NOT NULL) AS has_vec
            FROM events e
            LEFT JOIN event_vectors v ON v.event_id = e.id
            WHERE {_OLD_WHERE}
            ORDER BY e.date, e.id
            LIMIT %s
        """, (limit,))
        return [dict(row) for row in c.fetchall()]
    except Exception as e:
        logging.error(f"list_old_unchecked error: {e}")
        return []
    finally:
        conn.close()


def count_old_unchecked():
    """None if the base did not answer — the report then says «не удалось посчитать», not «0»."""
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"count_old_unchecked connect error: {e}")
        return None
    c = conn.cursor()
    try:
        c.execute(f"SELECT count(*) AS n FROM events e WHERE {_OLD_WHERE}")
        return int(c.fetchone()['n'])
    except Exception as e:
        logging.error(f"count_old_unchecked error: {e}")
        return None
    finally:
        conn.close()


def expire_past_dup_groups() -> int:
    """Groups whose day has passed and nobody decided: status 'expired'. Events are not touched.
    The add-bot shows only status 'wait', so the «🧩 Дубли (N)» counter stops growing with dead groups."""
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"expire_past_dup_groups connect error: {e}")
        return 0
    c = conn.cursor()
    try:
        c.execute(f"""
            UPDATE dup_groups SET status = 'expired', decided_at = now()
            WHERE status = 'wait' AND COALESCE(day, '') <> '' AND day < {_TODAY_MSK}
            RETURNING id
        """)
        n = len(c.fetchall())
        conn.commit()
        return n
    except Exception as e:
        conn.rollback()
        logging.error(f"expire_past_dup_groups error: {e}")
        return 0
    finally:
        conn.close()


def get_setting(key: str, default=None):
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"get_setting connect error: {e}")
        return default
    c = conn.cursor()
    try:
        c.execute('SELECT value FROM bot_settings WHERE key = %s', (key,))
        row = c.fetchone()
        return row['value'] if row and row['value'] is not None else default
    except Exception as e:
        logging.error(f"get_setting error ({key}): {e}")
        return default
    finally:
        conn.close()


def set_setting(key: str, value: str) -> bool:
    try:
        conn = get_conn()
    except Exception as e:
        logging.error(f"set_setting connect error: {e}")
        return False
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO bot_settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
        ''', (key, value))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"set_setting error ({key}): {e}")
        return False
    finally:
        conn.close()
