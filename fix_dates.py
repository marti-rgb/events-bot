# РАЗОВЫЙ СКРИПТ. Не часть обычного парсинга, запускается вручную одной кнопкой.
#
# Что делает:
# У части событий, сохранённых 26.08.2026 в 20:00 и позже, дата события
# сохранена с неверным годом (модель домыслила год, а не взяла его из даты
# публикации поста — баг починен в analyzer.py v42, но старые записи это не
# исправляет само по себе).
#
# Для каждой такой записи скрипт открывает исходный пост в Telegram
# (публичная страница, без токенов, бесплатно) и смотрит настоящую дату
# публикации:
#   — если пост реально свежий (17–26 августа 2026) — это подтверждает баг
#     с годом: чиним ТОЛЬКО год в дате события, день и месяц не трогаем.
#   — если пост на самом деле старый (просочился через дыру в фильтре,
#     которую мы закрыли в parser.py v44) — это не будущее событие, событие
#     помечается скрытым (is_ignored), но не удаляется физически.
#   — если пост не открылся (удалён, приватный канал и т.п.) — запись
#     остаётся как есть, попадает в список "на ручную проверку" в конце лога.
#
# Обращений к Z.ai / Groq / Cerebras нет вообще — только просмотр
# публичных страниц t.me и правка базы.

import time
import logging
import httpx
from bs4 import BeautifulSoup
from database import get_conn

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

# Граница между "реально старый пост" и "реально свежий пост" —
# та же, что использовалась при последнем прогоне парсера (PARSE_SINCE).
CUTOFF_DATE = '2026-08-17'

# Какие записи чиним — ровно тот диагностированный диапазон:
# события, сохранённые в последнем прогоне, с датой раньше 2026 года.
SELECT_SQL = """
    SELECT id, source_url, date, channel
    FROM events
    WHERE created_at >= '2026-08-26 20:00'
      AND date < '2026-01-01'
    ORDER BY id
"""


def fetch_real_post_date(client: httpx.Client, source_url: str) -> str | None:
    """Открывает публичную страницу поста и достаёт настоящую дату публикации."""
    url = source_url.rstrip('/') + '?embed=1'
    try:
        response = client.get(url, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, 'html.parser')
        time_tag = soup.find('time')
        if not time_tag or not time_tag.get('datetime'):
            return None
        return time_tag['datetime'][:10]
    except Exception as e:
        logging.error(f"Ошибка получения {source_url}: {e}")
        return None


def fix_year(conn, event_id: int, stored_date: str, real_year: str):
    """Меняет только год в дате события, день и месяц оставляет как есть."""
    new_date = real_year + stored_date[4:]
    c = conn.cursor()
    c.execute('UPDATE events SET date = %s WHERE id = %s', (new_date, event_id))
    conn.commit()
    logging.info(f"✅ id={event_id}: год починен {stored_date} → {new_date}")


def mark_ignored(conn, event_id: int, real_post_date: str):
    """Помечает событие скрытым — это оказался реально старый пост."""
    c = conn.cursor()
    c.execute(
        "UPDATE events SET is_ignored = true, ignored_reason = 'old_post_leaked_filter' WHERE id = %s",
        (event_id,)
    )
    conn.commit()
    logging.info(f"🚫 id={event_id}: скрыт как старый пост (настоящая публикация {real_post_date})")


def main():
    conn = get_conn()
    c = conn.cursor()
    c.execute(SELECT_SQL)
    rows = c.fetchall()
    conn.close()

    logging.info(f"Найдено записей на проверку: {len(rows)}")

    fixed_year = []
    marked_ignored = []
    failed = []

    with httpx.Client() as client:
        for row in rows:
            event_id = row['id']
            source_url = row['source_url']
            stored_date = row['date']

            real_date = fetch_real_post_date(client, source_url)
            time.sleep(1)

            if not real_date:
                failed.append((event_id, source_url))
                logging.warning(f"⚠️ id={event_id}: не удалось открыть {source_url}")
                continue

            conn2 = get_conn()
            if real_date >= CUTOFF_DATE:
                fix_year(conn2, event_id, stored_date, real_date[:4])
                fixed_year.append(event_id)
            else:
                mark_ignored(conn2, event_id, real_date)
                marked_ignored.append(event_id)
            conn2.close()

    logging.info("=" * 60)
    logging.info(f"Готово. Всего проверено: {len(rows)}")
    logging.info(f"Год починен: {len(fixed_year)} — id: {fixed_year}")
    logging.info(f"Скрыто как старые: {len(marked_ignored)} — id: {marked_ignored}")
    if failed:
        logging.info(f"Не открылось, нужна ручная проверка: {len(failed)}")
        for event_id, source_url in failed:
            logging.info(f"  id={event_id}: {source_url}")


if __name__ == '__main__':
    main()
