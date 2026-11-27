# post_retries.py — v2, 2026-11-27
# v2: Save retry progress per pass and claim one-time Telegram notices.
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


def retry_targets(include_review=False, pass_key=None):
    with closing(get_conn()) as conn, conn.cursor() as cursor:
        cursor.execute('''
            SELECT retry.channel, retry.post_id, retry.status,
                   EXISTS (SELECT 1 FROM public.events AS event
                           WHERE event.source_url = 'https://t.me/' || retry.channel || '/' || retry.post_id
                   ) AS already_saved
            FROM public.parser_post_retries AS retry
            WHERE retry.retry_at <= now() AND (retry.status = 'pending' OR (%s AND retry.status = 'review'))
              AND (%s::text IS NULL OR retry.last_retry_pass IS DISTINCT FROM %s)
            ORDER BY retry.retry_at, retry.channel, retry.post_id
        ''', (include_review, pass_key, pass_key))
        return cursor.fetchall()


def finish_retry(channel, post_id, status, stage, reason, attempts, pass_key=None):
    ready_at = _retry_at(attempts)
    with closing(get_conn()) as conn, conn, conn.cursor() as cursor:
        cursor.execute('''
            UPDATE public.parser_post_retries
            SET status = %s, stage = %s, reason = %s, attempts = %s::jsonb,
                retry_at = %s, retry_count = retry_count + 1, updated_at = now(),
                last_retry_pass = coalesce(%s, last_retry_pass)
            WHERE channel = %s AND post_id = %s AND status != 'resolved'
        ''', (status, stage, reason, json.dumps(attempts, ensure_ascii=False), ready_at,
              pass_key, channel, str(post_id)))


def pass_state(scope):
    with closing(get_conn()) as conn, conn.cursor() as cursor:
        cursor.execute('''
            SELECT started_at, completed_at IS NOT NULL AS channels_complete,
                   coalesce(retries_completed_at >= started_at, false) AS retries_complete
            FROM public.parser_passes WHERE scope = %s
        ''', (scope,))
        return cursor.fetchone()


def claim_summary(review_rows, pass_reference):
    with closing(get_conn()) as conn, conn, conn.cursor() as cursor:
        cursor.execute('SELECT clock_timestamp() AS stamp')
        stamp = cursor.fetchone()['stamp']
        if pass_reference:
            scope, started_at = pass_reference
            cursor.execute('''
                UPDATE public.parser_passes SET retries_completed_at = %s
                WHERE scope = %s AND started_at = %s AND completed_at IS NOT NULL
                  AND (retries_completed_at IS NULL OR retries_completed_at < started_at)
            ''', (stamp, scope, started_at))
            if cursor.rowcount != 1:
                raise RuntimeError('Не удалось подтвердить этап повторов текущего обхода')
        if review_rows:
            cursor.execute('''
                UPDATE public.parser_post_retries AS retry SET review_notified_at = %s
                FROM jsonb_to_recordset(%s::jsonb) AS message(channel text, post_id text)
                WHERE retry.channel = message.channel AND retry.post_id = message.post_id
                  AND retry.status = 'review' AND retry.review_notified_at IS NULL
            ''', (stamp, _review_keys(review_rows)))
            if cursor.rowcount != len(review_rows):
                raise RuntimeError('Список постов для сводки успел измениться')
        return stamp


def release_summary(review_rows, pass_reference, stamp):
    with closing(get_conn()) as conn, conn, conn.cursor() as cursor:
        if pass_reference:
            scope, started_at = pass_reference
            cursor.execute('''
                UPDATE public.parser_passes SET retries_completed_at = NULL
                WHERE scope = %s AND started_at = %s AND retries_completed_at = %s
            ''', (scope, started_at, stamp))
        if review_rows:
            cursor.execute('''
                UPDATE public.parser_post_retries AS retry SET review_notified_at = NULL
                FROM jsonb_to_recordset(%s::jsonb) AS message(channel text, post_id text)
                WHERE retry.channel = message.channel AND retry.post_id = message.post_id
                  AND retry.review_notified_at = %s
            ''', (_review_keys(review_rows), stamp))


def _review_keys(rows):
    return json.dumps([{'channel': row['channel'], 'post_id': row['post_id']} for row in rows])


def _retry_at(attempts):
    now = datetime.now(timezone.utc).timestamp()
    ready_at = max((item.get('retry_at', now) for item in attempts), default=now)
    return datetime.fromtimestamp(max(ready_at, now), timezone.utc)


def diagnostic_report():
    with closing(get_conn()) as conn, conn.cursor() as cursor:
        cursor.execute('''
            SELECT channel, post_id, status, stage, reason, first_attempts, attempts, retry_at, retry_count,
                   review_notified_at
            FROM public.parser_post_retries
            WHERE status != 'resolved'
               OR (updated_at AT TIME ZONE 'Europe/Moscow')::date = (now() AT TIME ZONE 'Europe/Moscow')::date
            ORDER BY status = 'resolved', updated_at, channel, post_id
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
        if row['status'] == 'review':
            report['отметка уведомления'] = row['review_notified_at'] is not None
        detail = json.dumps(report, ensure_ascii=False)
        print(detail, flush=True)
        if publish:
            log_parse(channel=row['channel'], post_id=row['post_id'], model=None, fallback=False,
                      success=row['status'] == 'resolved', stage='post_retry_diagnostic', error=detail)


if __name__ == '__main__':
    print_diagnostics(diagnostic_report())
