# dedup.py — v1
# v1 — task 56: semantic duplicates for the parser. Embeddings via Cloudflare Workers AI, candidates inside the
#      same day + city, AI judge for the grey band, groups for manual review in the add-bot. Nothing is hidden
#      while bot_settings.dup_auto is off. Created 21.10.2026
#
# GitHub secrets:   CF_ACCOUNT_ID, CF_AI_TOKEN
# GitHub variables: DUP_ON (1/0, default 1), DUP_MODEL (default @cf/baai/bge-m3), DUP_FILL_MAX (default 400)
# Settings in bot_settings (changed in the database, no code edit): dup_hi, dup_lo, dup_auto, dup_ai_budget

import os
import json
import logging

import httpx

from analyzer import zai_client, groq_client, _chat, _strip_fence
import database as db

CF_ACCOUNT_ID = (os.getenv('CF_ACCOUNT_ID') or '').strip()
CF_AI_TOKEN = (os.getenv('CF_AI_TOKEN') or '').strip()
DUP_MODEL = (os.getenv('DUP_MODEL') or '@cf/baai/bge-m3').strip()
DUP_ON = (os.getenv('DUP_ON', '1') or '1').strip().lower() in ('1', 'true', 'yes', 'on')

try:
    DUP_FILL_MAX = int(os.getenv('DUP_FILL_MAX', '400') or 400)
except ValueError:
    DUP_FILL_MAX = 400

DESC_CHARS = 600         # how much of the description goes into the vector
JUDGE_DESC_CHARS = 300   # how much of the description goes to the judge
CAND_LIMIT = 5           # nearest neighbours asked from the database per new event
FILL_PER_EVENT = 20      # old events of the same day+city that get a vector alongside one new event
EMBED_TIMEOUT = 30

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

Ответь ТОЛЬКО JSON, без пояснений и без markdown:
{"same": true/false, "why": "одна короткая фраза по-русски, не больше 12 слов"}

Мероприятие A:
%(a)s

Мероприятие B:
%(b)s"""

# Counters and problems of the current run (read by finish_run()).
STATS = {
    'checked': 0,      # new events that went through the check
    'vectors': 0,      # vectors computed (new events + backfill)
    'filled': 0,       # vectors computed for events that were already in the base
    'pairs_hi': 0,     # pairs above dup_hi — duplicates without asking the AI
    'pairs_grey': 0,   # pairs inside the grey band
    'judged': 0,       # grey pairs actually sent to the judge
    'judged_yes': 0,
    'grey_left': 0,    # grey pairs left for the next run (budget spent)
    'groups_new': 0,
    'groups_join': 0,
    'hidden': 0,       # hidden right away (only while dup_auto is on)
    'errors': 0,
}
PROBLEMS = []          # one line per problem kind, shown to the admin once per run

_settings = None
_off_reason = None     # set once when the whole module turns itself off for this run


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
                     f"бюджет судьи {_settings['ai_budget']}")
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
            response = _chat(provider, client, model, prompt, 200, 0.0, False)
            raw = _strip_fence(response.choices[0].message.content or '')
            data = json.loads(raw)
            same = bool(data.get('same'))
            why = (data.get('why') or '').strip()[:200]
            return same, why, f"{provider}/{model}"
        except Exception as e:
            logging.warning(f"Дедуп, судья {provider}/{model} не ответил: {e}")
            continue
    _problem('Судья', 'ни одна модель не ответила на вопрос о дубле — пары ждут следующего прогона')
    return None, '', ''


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
        STATS['checked'] += 1

        await _fill_missing_vectors(day, city)

        candidates = db.find_dup_candidates(event_id, CAND_LIMIT, st['lo'])
        for cand in candidates:
            score = float(cand.get('score') or 0)
            by_ai = False
            reason = ''
            if score >= st['hi']:
                STATS['pairs_hi'] += 1
                reason = f"похожесть {score:.2f}"
            elif score >= st['lo']:
                STATS['pairs_grey'] += 1
                if STATS['judged'] >= st['ai_budget']:
                    STATS['grey_left'] += 1
                    continue
                STATS['judged'] += 1
                same, why, model = _ask_judge(ev, cand)
                if not same:
                    continue
                STATS['judged_yes'] += 1
                by_ai = True
                reason = why or f"нейросеть: одно и то же ({model})"
            else:
                continue

            joined, group_id = db.create_or_join_group(day, city, event_id, cand['id'], score, by_ai, reason)
            if not group_id:
                continue
            STATS['groups_join' if joined else 'groups_new'] += 1
            if st['auto']:
                STATS['hidden'] += db.hide_losers(group_id)
            # one duplicate per event is enough: the rest of the group is found through the group itself
            break

        db.mark_dup_checked(event_id)
    except Exception as e:
        logging.error(f"dedup.process error (event {event_id}): {e}")
        _problem('Дедуп', f'сбой проверки мероприятия {event_id}: {e}')


def report_line() -> str:
    """One line for the parser log and for the admin message."""
    return (f"🧩 Дубли: проверено {STATS['checked']}, векторов {STATS['vectors']} "
            f"(догружено старым {STATS['filled']}), явных пар {STATS['pairs_hi']}, "
            f"спорных {STATS['pairs_grey']} (разобрано судьёй {STATS['judged']}, "
            f"из них дубли {STATS['judged_yes']}, осталось на завтра {STATS['grey_left']}), "
            f"групп новых {STATS['groups_new']}, дописано в старые {STATS['groups_join']}"
            + (f", скрыто автоматом {STATS['hidden']}" if STATS['hidden'] else ''))


async def finish_run(notify):
    """Called once at the end of the parser run. Sends the summary and the problems, if any."""
    if not DUP_ON:
        return
    line = report_line()
    logging.info(line)
    if PROBLEMS:
        await notify("⚠️ Дубли: " + "\n".join(PROBLEMS) + f"\n\n{line}\nМероприятия сохранялись как обычно.")
    elif STATS['groups_new'] or STATS['groups_join']:
        await notify(line + "\n\nПосмотреть и подтвердить — в add-боте, «🧩 Дубли».")
