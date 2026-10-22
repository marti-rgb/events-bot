# dedup.py — v5
# v5 — task 56: in the report of the OLD-events check the line «векторов N» showed the parser pass counter and so
#      was always 0 — run_old() counts the vectors it builds in another one. Numbers only, nothing else changed.
#      22.10.2026
# v4 — task 56: the judge's answer ceiling is 200 → 1500 tokens (variable DUP_JUDGE_TOKENS). gpt-oss-120b reasons
#      before answering and the reasoning ate the 200, so Groq returned empty or truncated JSON («Expecting value»,
#      «Unterminated string») and every pair went to the slower reserve — about 90 s instead of 25 s per pair, and
#      the free Groq day was spent on answers nobody could read. Reasoning is left on (decision 21.10). 22.10.2026
#
# GitHub secrets:   CF_ACCOUNT_ID, CF_AI_TOKEN
# GitHub variables: DUP_ON (1/0, default 1), DUP_MODEL (default @cf/baai/bge-m3), DUP_FILL_MAX (default 400),
#                   DUP_OLD_MAX (default 3000), DUP_JUDGE_GAP (seconds, default 8), DUP_EMBED_BATCH (default 50),
#                   DUP_JUDGE_TOKENS (default 1500)
# Settings in bot_settings (changed in the database, no code edit): dup_hi, dup_lo, dup_auto, dup_ai_budget,
#                   dup_old_ai_budget; dup_old_done is written by the code (the «all old checked» message went out)

import os
import re
import json
import time
import asyncio
import logging

import httpx

from analyzer import zai_client, groq_client, _chat, _strip_fence
import database as db

CF_ACCOUNT_ID = (os.getenv('CF_ACCOUNT_ID') or '').strip()
CF_AI_TOKEN = (os.getenv('CF_AI_TOKEN') or '').strip()
DUP_MODEL = (os.getenv('DUP_MODEL') or '@cf/baai/bge-m3').strip()
DUP_ON = (os.getenv('DUP_ON', '1') or '1').strip().lower() in ('1', 'true', 'yes', 'on')


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


DUP_FILL_MAX = _env_int('DUP_FILL_MAX', 400)
DUP_OLD_MAX = _env_int('DUP_OLD_MAX', 3000)      # old events looked at in one run_old()
JUDGE_GAP = _env_int('DUP_JUDGE_GAP', 8)         # seconds between two judge questions

DESC_CHARS = 600         # how much of the description goes into the vector
JUDGE_DESC_CHARS = 300   # how much of the description goes to the judge
CAND_LIMIT = 5           # nearest neighbours asked from the database per event
FILL_PER_EVENT = 20      # old events of the same day+city that get a vector alongside one new event
EMBED_TIMEOUT = 30
EMBED_BATCH = max(1, _env_int('DUP_EMBED_BATCH', 50))   # texts per Cloudflare request in run_old()
# The answer itself is ~30 tokens, but gpt-oss-120b reasons before answering and the reasoning counts toward the
# same ceiling: with 200 (v1–v3) Groq kept returning an empty or half-written JSON and every pair fell through to
# the slower reserve. A ceiling is not spent unless used, so a high one costs nothing.
JUDGE_MAX_TOKENS = _env_int('DUP_JUDGE_TOKENS', 1500)

DUP_SCREEN_LINK = 'https://t.me/weekendy_add_bot?start=dup'   # opens «🧩 Дубли» (add v83)

# Giveaway post vs the plain announcement of the same show: for a viewer these are two different things.
# Only the TITLE is looked at — a festival description may mention «розыгрыш призов» in its programme.
GIVEAWAY_TITLE_RE = re.compile(r'розыгрыш|разыгр|giveaway|\bдарим\b', re.IGNORECASE)

# The judge: the strongest free model first. 4.5 is last on purpose — it is known to answer
# differently on identical text (exhibition «Искусство сцены», 32 copies).
JUDGE_CHAIN = [
    ('groq', groq_client, 'openai/gpt-oss-120b'),
    ('zai', zai_client, 'glm-4.7-flash'),
    ('zai', zai_client, 'glm-4.5-flash'),
]

JUDGE_PROMPT = """Два анонса мероприятий из разных Telegram-каналов. Реши: это одно и то же мероприятие или два разных.

ОДНО И ТО ЖЕ — один и тот же сеанс: та же площадка, то же время, то же событие, просто описано разными словами.
РАЗНЫЕ — разные сеансы одного спектакля, разные части цикла, разные площадки, разные программы.
РАЗНЫЕ — если один пост про розыгрыш билетов, конкурс или подарок, а другой — обычный анонс, даже если сеанс тот же.

Ответь ТОЛЬКО JSON, без пояснений и без markdown:
{"same": true/false, "why": "одна короткая фраза по-русски, не больше 12 слов"}

Мероприятие A:
%(a)s

Мероприятие B:
%(b)s"""

# Counters and problems of the current run (read by finish_run() / run_old()).
STATS = {
    'checked': 0,      # events that went through the check (and were marked)
    'vectors': 0,      # vectors computed (new events + backfill)
    'filled': 0,       # vectors computed for events that were already in the base
    'pairs_hi': 0,     # pairs above dup_hi — duplicates without asking the AI
    'pairs_grey': 0,   # pairs inside the grey band
    'judged': 0,       # grey pairs actually sent to the judge
    'judged_yes': 0,
    'grey_left': 0,    # grey pairs left for the next run (budget spent)
    'giveaway': 0,     # pairs skipped: giveaway vs announcement
    'groups_new': 0,
    'groups_join': 0,
    'groups_merged': 0,  # v3: two piles of the same event joined into one group
    'hidden': 0,       # hidden right away (only while dup_auto is on)
    'errors': 0,
    # run_old() only
    'old_seen': 0,     # old events taken into this run
    'old_kept': 0,     # left unchecked for tomorrow (budget spent or no vector)
    'expired': 0,      # past waiting groups closed
}
JUDGE_MODELS = {}      # which model answered how many times
PROBLEMS = []          # one line per problem kind, shown to the admin once per run

_settings = None
_off_reason = None     # set once when the whole module turns itself off for this run
_last_judge_at = 0.0


def _problem(kind: str, text: str):
    """One line per problem kind per run — the admin gets a short list, not a flood."""
    STATS['errors'] += 1
    for p in PROBLEMS:
        if p.startswith(kind):
            return
    PROBLEMS.append(f"{kind}: {text}"[:300])


def settings() -> dict:
    """bot_settings values, read once per run."""
    global _settings
    if _settings is None:
        _settings = db.get_dup_settings()
        logging.info(f"Дедуп: пороги {_settings['lo']}–{_settings['hi']}, "
                     f"авторежим {'вкл' if _settings['auto'] else 'выкл'}, "
                     f"бюджет судьи {_settings['ai_budget']} (старые {_settings['old_ai_budget']})")
    return _settings


def enabled() -> bool:
    global _off_reason
    if not DUP_ON:
        return False
    if not CF_ACCOUNT_ID or not CF_AI_TOKEN:
        if _off_reason is None:
            _off_reason = 'нет ключей Cloudflare (CF_ACCOUNT_ID / CF_AI_TOKEN) — дубли в этом прогоне не ищутся'
            _problem('Cloudflare', _off_reason)
        return False
    return True


def event_text(ev: dict) -> str:
    """What goes into the vector: no price, no links, no channel — they only add noise."""
    parts = [
        (ev.get('title') or '').strip(),
        (ev.get('location') or '').strip(),
        (ev.get('address') or '').strip(),
        (ev.get('description') or '').strip()[:DESC_CHARS],
    ]
    return '\n'.join(p for p in parts if p)[:4000]


def judge_text(ev: dict) -> str:
    rows = [
        f"Название: {(ev.get('title') or '—').strip()}",
        f"Дата: {(ev.get('date') or '—')} {(ev.get('time') or '').strip()}".strip(),
        f"Место: {(ev.get('location') or '—').strip()}",
        f"Адрес: {(ev.get('address') or '—').strip()}",
        f"Цена: {(ev.get('price') or 'не указана')}",
        f"Описание: {(ev.get('description') or '').strip()[:JUDGE_DESC_CHARS]}",
    ]
    return '\n'.join(rows)


def is_giveaway(ev: dict) -> bool:
    return bool(GIVEAWAY_TITLE_RE.search(ev.get('title') or ''))


async def embed(texts: list[str]) -> list[list[float]] | None:
    """One request to Cloudflare for a batch of texts. None on any failure — the caller just skips the check."""
    if not texts:
        return []
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{DUP_MODEL}"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                url,
                headers={'Authorization': f'Bearer {CF_AI_TOKEN}'},
                json={'text': texts},
                timeout=EMBED_TIMEOUT,
            )
        if r.status_code == 401 or r.status_code == 403:
            _problem('Cloudflare', f'ключ не принят (HTTP {r.status_code}) — проверьте CF_AI_TOKEN')
            return None
        if r.status_code == 429:
            _problem('Cloudflare', 'кончилась дневная бесплатная норма (HTTP 429) — дубли досчитаются завтра')
            return None
        if r.status_code != 200:
            _problem('Cloudflare', f'HTTP {r.status_code}: {r.text[:150]}')
            return None
        data = r.json()
        vecs = (data.get('result') or {}).get('data')
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            _problem('Cloudflare', f'непонятный ответ модели: {str(data)[:150]}')
            return None
        STATS['vectors'] += len(vecs)
        return vecs
    except Exception as e:
        _problem('Cloudflare', f'запрос не прошёл: {e}')
        return None


def _ask_judge(a: dict, b: dict):
    """Returns (same: bool, why: str, model: str) or (None, '', '') if no model answered."""
    prompt = JUDGE_PROMPT % {'a': judge_text(a), 'b': judge_text(b)}
    for provider, client, model in JUDGE_CHAIN:
        if client is None:
            continue
        try:
            response = _chat(provider, client, model, prompt, JUDGE_MAX_TOKENS, 0.0, False)
            raw = _strip_fence(response.choices[0].message.content or '')
            data = json.loads(raw)
            same = bool(data.get('same'))
            why = (data.get('why') or '').strip()[:200]
            name = f"{provider}/{model}"
            JUDGE_MODELS[name] = JUDGE_MODELS.get(name, 0) + 1
            return same, why, name
        except Exception as e:
            logging.warning(f"Дедуп, судья {provider}/{model} не ответил: {e}")
            continue
    _problem('Судья', 'ни одна модель не ответила на вопрос о дубле — пары ждут следующего прогона')
    return None, '', ''


async def _judge(a: dict, b: dict):
    """v2: keeps JUDGE_GAP seconds between questions. In the parser the posts themselves take ~15 s,
    so it almost never waits there; in run_old() the questions would otherwise go back to back."""
    global _last_judge_at
    wait = JUDGE_GAP - (time.monotonic() - _last_judge_at)
    if wait > 0:
        await asyncio.sleep(wait)
    try:
        return _ask_judge(a, b)
    finally:
        _last_judge_at = time.monotonic()


async def _fill_missing_vectors(day: str, city: str):
    """Events already in the base have no vectors — without them there is nothing to compare with.
    Alongside every new event we quietly give vectors to its neighbours of the same day and city."""
    if STATS['filled'] >= DUP_FILL_MAX:
        return
    left = min(FILL_PER_EVENT, DUP_FILL_MAX - STATS['filled'])
    rows = db.find_events_without_vector(day, city, left)
    if not rows:
        return
    vecs = await embed([event_text(r) for r in rows])
    if not vecs:
        return
    for row, vec in zip(rows, vecs):
        if db.save_event_vector(row['id'], vec, DUP_MODEL):
            STATS['filled'] += 1


async def _check_pairs(event_id: int, ev: dict, day: str, city, budget: int, only_checked: bool) -> bool:
    """Looks for this event's duplicates among its nearest neighbours and puts them into one group.
    v3: goes through ALL neighbours (up to CAND_LIMIT), not just until the first match — stopping early let copies
    of one event split into separate piles that were approved separately and both stayed visible. A neighbour
    already in my group (after a join or a merge) is skipped without asking the judge.
    Returns False when a grey pair had to be skipped (judge budget spent, no judge answered, group write failed) —
    the event then stays unchecked and run_old() comes back to it the next day."""
    st = settings()
    complete = True
    linked = set()   # ids already in my group
    allowed = None   # after a link: neighbours still allowed (the time rule now sees my group)
    candidates = db.find_dup_candidates(event_id, CAND_LIMIT, st['lo'], only_checked)
    for cand in candidates:
        if cand['id'] in linked:
            continue
        if allowed is not None and cand['id'] not in allowed:
            continue
        if is_giveaway(ev) != is_giveaway(cand):
            STATS['giveaway'] += 1
            continue
        score = float(cand.get('score') or 0)
        by_ai = False
        reason = ''
        if score >= st['hi']:
            STATS['pairs_hi'] += 1
            reason = f"похожесть {score:.2f}"
        elif score >= st['lo']:
            STATS['pairs_grey'] += 1
            if STATS['judged'] >= budget:
                STATS['grey_left'] += 1
                complete = False
                continue
            STATS['judged'] += 1
            same, why, model = await _judge(ev, cand)
            if same is None:
                complete = False
                continue
            if not same:
                db.record_not_dup(day, city, event_id, cand['id'], why)   # v3: never ask about this pair again
                continue
            STATS['judged_yes'] += 1
            by_ai = True
            reason = why or f"нейросеть: одно и то же ({model})"
        else:
            continue

        res = db.create_or_join_group(day, city, event_id, cand['id'], score, by_ai, reason)
        if not res:
            complete = False
            continue
        if not linked:   # the first link of this event: a new group or a join; later links only add to it
            STATS['groups_join' if res['joined'] else 'groups_new'] += 1
        STATS['groups_merged'] += res['merged']
        linked |= res['members']
        if st['auto']:
            STATS['hidden'] += db.hide_losers(res['group_id'])
        # My group may now hold a start time (19:00) that a later neighbour's group contradicts (21:00) —
        # ask the database again which of the remaining neighbours are still allowed.
        allowed = {c['id'] for c in db.find_dup_candidates(event_id, CAND_LIMIT, st['lo'], only_checked)}
    return complete


async def process(event_id: int, ev: dict):
    """Called by the parser right after an event was saved. Never raises: the parser must not stop because of dubles."""
    if not enabled() or not event_id:
        return
    try:
        st = settings()
        city = db.event_city(event_id)
        day = (ev.get('date') or '').strip()
        if not day:
            return

        vecs = await embed([event_text(ev)])
        if not vecs:
            return
        if not db.save_event_vector(event_id, vecs[0], DUP_MODEL):
            return

        await _fill_missing_vectors(day, city)

        if await _check_pairs(event_id, ev, day, city, st['ai_budget'], only_checked=False):
            STATS['checked'] += 1
            db.mark_dup_checked(event_id)
    except Exception as e:
        logging.error(f"dedup.process error (event {event_id}): {e}")
        _problem('Дедуп', f'сбой проверки мероприятия {event_id}: {e}')


def _judge_models_text() -> str:
    if not JUDGE_MODELS:
        return ''
    return '; судья: ' + ', '.join(f"{k.split('/', 1)[-1]} {v}" for k, v in JUDGE_MODELS.items())


def report_line() -> str:
    """One line for the parser log and for the admin message."""
    return (f"🧩 Дубли: проверено {STATS['checked']}, векторов {STATS['vectors']} "
            f"(догружено старым {STATS['filled']}), явных пар {STATS['pairs_hi']}, "
            f"спорных {STATS['pairs_grey']} (разобрано судьёй {STATS['judged']}, "
            f"из них дубли {STATS['judged_yes']}, осталось на завтра {STATS['grey_left']}), "
            f"групп новых {STATS['groups_new']}, дописано в старые {STATS['groups_join']}"
            + (f", слито групп {STATS['groups_merged']}" if STATS['groups_merged'] else '')
            + (f", розыгрыш≠анонс {STATS['giveaway']}" if STATS['giveaway'] else '')
            + (f", скрыто автоматом {STATS['hidden']}" if STATS['hidden'] else '')
            + _judge_models_text())


async def finish_run(notify):
    """Called once at the end of the parser run. Sends the summary and the problems, if any."""
    if not DUP_ON:
        return
    line = report_line()
    logging.info(line)
    if PROBLEMS:
        await notify("⚠️ Дубли: " + "\n".join(PROBLEMS) + f"\n\n{line}\nМероприятия сохранялись как обычно.")
    elif STATS['groups_new'] or STATS['groups_join'] or STATS['groups_merged']:
        await notify(line + f"\n\nПосмотреть и подтвердить: {DUP_SCREEN_LINK}")


# ===== v2: old events =====

async def _old_vectors(rows: list) -> set:
    """Vectors for old events that have none, in batches. Returns the ids that have a vector afterwards."""
    ready = {r['id'] for r in rows if r.get('has_vec')}
    todo = [r for r in rows if not r.get('has_vec')]
    for i in range(0, len(todo), EMBED_BATCH):
        part = todo[i:i + EMBED_BATCH]
        vecs = await embed([event_text(r) for r in part])
        if not vecs:
            break   # Cloudflare refused — the problem is already noted; the rest waits for tomorrow
        saved = db.save_event_vectors_bulk([(r['id'], v) for r, v in zip(part, vecs)], DUP_MODEL)
        if saved:
            STATS['filled'] += saved
            ready.update(r['id'] for r in part)
        else:
            _problem('База', 'векторы старых мероприятий не сохранились — проверка продолжится завтра')
            break
    return ready


def old_report_line(left) -> str:
    left_txt = 'не удалось посчитать' if left is None else str(left)
    return (f"🧩 Дубли среди старых: проверено {STATS['checked']}, осталось {left_txt}; "
            f"векторов {STATS['filled']}, явных пар {STATS['pairs_hi']}, "
            f"спорных {STATS['pairs_grey']} (разобрано судьёй {STATS['judged']}, "
            f"из них дубли {STATS['judged_yes']}, на завтра {STATS['grey_left']}), "
            f"групп новых {STATS['groups_new']}, дописано в старые {STATS['groups_join']}"
            + (f", слито групп {STATS['groups_merged']}" if STATS['groups_merged'] else '')
            + (f", розыгрыш≠анонс {STATS['giveaway']}" if STATS['giveaway'] else '')
            + (f", скрыто автоматом {STATS['hidden']}" if STATS['hidden'] else '')
            + (f", закрыто прошедших групп {STATS['expired']}" if STATS['expired'] else '')
            + _judge_models_text())


async def run_old(notify):
    """The daily check of old events (dedup_old.py). Never raises."""
    if not DUP_ON:
        logging.info("DUP_ON=0 — проверка старых выключена")
        return
    try:
        if not enabled():
            await notify("⚠️ Дубли среди старых: " + "\n".join(PROBLEMS))
            return
        st = settings()
        STATS['expired'] = db.expire_past_dup_groups()

        rows = db.list_old_unchecked(DUP_OLD_MAX)
        STATS['old_seen'] = len(rows)
        logging.info(f"Старые без проверки: взято {len(rows)}")
        ready = await _old_vectors(rows) if rows else set()

        for row in rows:
            if row['id'] not in ready:
                STATS['old_kept'] += 1
                continue
            day = (row.get('date') or '').strip()
            try:
                if await _check_pairs(row['id'], row, day, row.get('city'), st['old_ai_budget'], only_checked=True):
                    STATS['checked'] += 1
                    db.mark_dup_checked(row['id'])
                else:
                    STATS['old_kept'] += 1
            except Exception as e:
                STATS['old_kept'] += 1
                logging.error(f"dedup.run_old error (event {row['id']}): {e}")
                _problem('Дедуп', f'сбой проверки мероприятия {row["id"]}: {e}')

        left = db.count_old_unchecked()
        line = old_report_line(left)
        logging.info(line)

        # One message per run at most. During the first full pass — progress every day; after it — only when
        # something was found or broke. «All checked» is said once (bot_settings.dup_old_done).
        done_before = (db.get_setting('dup_old_done', '') or '') != ''
        finished_now = left == 0 and not done_before
        if finished_now:
            db.set_setting('dup_old_done', time.strftime('%Y-%m-%d'))
        found = STATS['groups_new'] or STATS['groups_join'] or STATS['groups_merged']
        text = ''
        if PROBLEMS:
            text = "⚠️ Дубли среди старых: " + "\n".join(PROBLEMS) + f"\n\n{line}"
        elif found or finished_now or (not done_before and STATS['checked']):
            text = line
        if text and found:
            text += f"\n\nПосмотреть и подтвердить: {DUP_SCREEN_LINK}"
        if text and finished_now:
            text += ("\n\n✅ Все старые мероприятия проверены на дубли. Дальше проверка идёт сама каждый день "
                     "и пишет, только если что-то нашла или что-то сломалось.")
        if text:
            await notify(text)
    except Exception as e:
        logging.error(f"dedup.run_old crashed: {e}")
        await notify(f"⚠️ Дубли среди старых: проверка упала — {e}")
