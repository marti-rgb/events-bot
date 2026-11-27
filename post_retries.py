# post_retries.py — v1, 2026-11-27
# v1: Persist unfinished posts and report the outcomes of real parser attempts.
import json
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from database import get_conn, log_parse


def is_known_post(channel, post_id):
    with closing(get_conn()) as conn, conn.cursor() as cursor:
        cursor.execute('''
            SELECT EXISTS (
                SELECT 1 FROM public.processed_posts WHERE channel = %s AND message_id = %s
                UNION ALL
                SELECT 1 FROM public.parser_post_retries WHERE channel = %s AND post_id = %s
            ) AS known
        ''', (channel, int(post_id), channel, str(post_id)))
        return cursor.fetchone()['known']


def defer_post(channel, post_id, stage, reason, attempts):
    ready_at = _retry_at(attempts)
    with closing(get_conn()) as conn, conn, conn.cursor() as cursor:
        cursor.execute('''
            INSERT INTO public.parser_post_retries (channel, post_id, status, stage, reason, first_attempts, retry_at)
            VALUES (%s, %s, 'pending', %s, %s, %s::jsonb, %s)
            ON CONFLICT (channel, post_id) DO NOTHING
        ''', (channel, str(post_id), stage, reason, json.dumps(attempts, ensure_ascii=False), ready_at))


def retry_targets(include_review=False):
    with closing(get_conn()) as conn, conn.cursor() as cursor:
        cursor.execute('''
            SELECT retry.channel, retry.post_id, retry.status,
                   EXISTS (SELECT 1 FROM public.events AS event
                           WHERE event.source_url = 'https://t.me/' || retry.channel || '/' || retry.post_id
                   ) AS already_saved
            FROM public.parser_post_retries AS retry
            WHERE retry.retry_at <= now() AND (retry.status = 'pending' OR (%s AND retry.status = 'review'))
            ORDER BY retry.retry_at, retry.channel, retry.post_id
        ''', (include_review,))
        return cursor.fetchall()


def finish_retry(channel, post_id, status, stage, reason, attempts):
    ready_at = _retry_at(attempts)
    with closing(get_conn()) as conn, conn, conn.cursor() as cursor:
        cursor.execute('''
            UPDATE public.parser_post_retries
            SET status = %s, stage = %s, reason = %s, attempts = %s::jsonb,
                retry_at = %s, retry_count = retry_count + 1, updated_at = now()
            WHERE channel = %s AND post_id = %s AND status != 'resolved'
        ''', (status, stage, reason, json.dumps(attempts, ensure_ascii=False), ready_at, channel, str(post_id)))


def _retry_at(attempts):
    now = datetime.now(timezone.utc).timestamp()
    ready_at = max((item.get('retry_at', now) for item in attempts), default=now)
    return datetime.fromtimestamp(max(ready_at, now), timezone.utc)


def diagnostic_report():
    with closing(get_conn()) as conn, conn.cursor() as cursor:
        cursor.execute('''
            SELECT channel, post_id, status, stage, reason, first_attempts, attempts, retry_at, retry_count
            FROM public.parser_post_retries
            WHERE status != 'resolved'
               OR (updated_at AT TIME ZONE 'Europe/Moscow')::date = (now() AT TIME ZONE 'Europe/Moscow')::date
            ORDER BY status = 'resolved', channel, post_id
        ''')
        return cursor.fetchall()


def print_diagnostics(rows, *, publish=False):
    status_names = {'pending': 'Ждёт повторного разбора', 'review': 'Остался в диагностике', 'resolved': 'Обработка завершена'}
    stage_names = {'screen': 'Предварительный отбор', 'analyze': 'Разбор поста', 'fetch': 'Чтение Telegram',
                   'save': 'Сохранение', 'retry': 'Повторный разбор'}
    print(f'Диагностика повторного разбора — {datetime.now(ZoneInfo("Europe/Moscow")):%d.%m.%Y} МСК', flush=True)
    print(f'Постов в отчёте: {len(rows)}', flush=True)
    for row in rows:
        url = f'https://t.me/{row["channel"]}/{row["post_id"]}'
        report = {
            'ссылка': url,
            'итог': status_names[row['status']],
            'этап': stage_names[row['stage']],
            'причина': row['reason'],
            'повторов': row['retry_count'],
            'модели при первом сбое': row['first_attempts'],
            'модели при последнем повторе': row['attempts'],
        }
        if row['status'] == 'pending':
            report['повтор не раньше'] = row['retry_at'].astimezone(ZoneInfo('Europe/Moscow')).isoformat(timespec='seconds')
        detail = json.dumps(report, ensure_ascii=False)
        print(detail, flush=True)
        if publish:
            log_parse(channel=row['channel'], post_id=row['post_id'], model=None, fallback=False,
                      success=row['status'] == 'resolved', stage='post_retry_diagnostic', error=detail)


if __name__ == '__main__':
    print_diagnostics(diagnostic_report())
