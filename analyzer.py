# analyzer.py — ВЕРСИЯ v45 — 14.10.2026
# v45 — sections (L1) are always brought to the справочник: the AI's answer is fitted by spelling (case, word order,
# part of the name: «искусство и культура» → «Культура и искусство», «Бизнес» → «Бизнес и нетворкинг»); if nothing fits
# and the post is an event — a short second request asks the AI to pick 1–2 sections from the list; if no model answers,
# the post is returned as not processed (retried next run) instead of being thrown away. Every fit is logged. Updated 14.10.2026
# v44 — правило цены в промпте: «бесплатно» только если бесплатен вход/участие для всех;
# бесплатно с условием («для новых клиентов», «первым N», «по промокоду», «при покупке»,
# «первое/пробное занятие бесплатно», «для подписчиков/участников клуба») → is_free = null
# (цена не указана), если цены нет; слово «бесплатно» про подарок/консультацию/разбор/бонус
# не делает мероприятие бесплатным; одна только регистрация/запись без цены → null, а не false
# (у бесплатных мероприятий тоже бывает регистрация). Случай 26984: «Бесплатно для новых
# клиентов центра…» — на сайте 7500 ₽. Обновлено 11.10.2026
#
# История версий (старше двух последних) — в versions_history.md
# Переменные в GitHub → Settings → Variables:
#   ANALYZE_THINKING=0  PARSE_PAGES=1  USE_SCREEN=0  CHANNELS_PART=(пусто)
import os
import re
import json
import time
import logging
from database import log_parse
from openai import OpenAI

# Cerebras отключён 17.08.2026 — бесплатный доступ закрыт, требуется карта.
# Оставлено закомментированным на случай возврата.
# cerebras_client = OpenAI(
#     base_url="https://api.cerebras.ai/v1",
#     api_key=os.getenv('CEREBRAS_API_KEY')
# )

zai_client = None
if os.getenv('ZAI_API_KEY'):
    zai_client = OpenAI(
        base_url="https://api.z.ai/api/paas/v4",
        api_key=os.getenv('ZAI_API_KEY')
    )

groq_client = None
try:
    from groq import Groq
    groq_client = Groq(api_key=os.getenv('GROQ_API_KEY'))
except:
    pass

# Размышления для второй линии (analyze_post).
# Переключается переменной ANALYZE_THINKING в GitHub → Settings → Variables.
ANALYZE_THINKING = os.getenv('ANALYZE_THINKING', '0').strip().lower() in ('1', 'true', 'yes', 'on')

# Порядок обращения к моделям. Первая живая отвечает — остальные не трогаем.
SCREEN_CHAIN = [
    ('zai', zai_client, 'glm-4.5-flash'),
    ('groq', groq_client, 'openai/gpt-oss-20b'),
]

ANALYZE_CHAIN = [
    ('zai', zai_client, 'glm-4.5-flash'),
    ('groq', groq_client, 'openai/gpt-oss-120b'),
    ('groq', groq_client, 'openai/gpt-oss-20b'),
]


def _is_rate_limit(err) -> bool:
    s = str(err).lower()
    return '429' in s or 'rate limit' in s or 'too many requests' in s


def _retry_after(err, default=5.0, cap=15.0) -> float:
    """Сколько ждать перед повтором: берём из ответа сервера, если он подсказал."""
    seconds = default
    try:
        headers = getattr(getattr(err, 'response', None), 'headers', None) or {}
        raw = headers.get('retry-after') or headers.get('Retry-After')
        if raw:
            seconds = float(str(raw).strip())
    except Exception:
        seconds = default
    return max(0.5, min(seconds, cap))


def _chat(provider, client, model, prompt, max_tokens, temperature, thinking):
    kwargs = dict(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if provider == 'zai':
        kwargs['extra_body'] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
    return client.chat.completions.create(**kwargs)


def _strip_fence(raw: str) -> str:
    raw = (raw or '').strip()
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
    return raw.strip()


# ===== patch 2 (v45): fit the AI's sections (L1) to the справочник by spelling =====
# Same section written differently: case, ё/е, word order, part of the name. Words shorter than 3 letters («и») are ignored;
# words are compared by their first 5 letters («Выставка» = «Выставки», «Музыкальные» = «Музыка»).
# Exactly one candidate — taken; none or several — not guessed (patch 3 decides).
def _l1_words(s) -> set:
    s = str(s or '').lower().replace('ё', 'е')
    return {w[:5] for w in re.split(r'[^a-zа-я0-9]+', s) if len(w) >= 3}


def _match_l1(value, valid_l1: list):
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().lower().replace('ё', 'е')
    for c in valid_l1:
        if c.lower().replace('ё', 'е') == v:
            return c
    vw = _l1_words(value)
    if not vw:
        return None
    same = [c for c in valid_l1 if _l1_words(c) == vw]
    if len(same) == 1:
        return same[0]
    part = [c for c in valid_l1 if vw <= _l1_words(c)]
    if len(part) == 1:
        return part[0]
    return None


def _fit_l1(raw, valid_l1: list) -> tuple[list, list]:
    """Returns (sections from the справочник, values that did not fit)."""
    if not isinstance(raw, list):
        raw = [raw] if raw else []
    fitted, left = [], []
    for x in raw:
        m = _match_l1(x, valid_l1)
        if m is None:
            left.append(x)
            continue
        if m != x:
            logging.info(f"Категория подогнана: «{x}» → «{m}»")
        if m not in fitted:
            fitted.append(m)
    return fitted, left


# ===== patch 3 (v45): the AI picks the section by meaning when nothing fitted =====
PICK_L1_PROMPT = """К какому разделу относится это мероприятие? Выбери 1–2 раздела СТРОГО из списка, слово в слово.
Список разделов: {l1_list}
Если ни один раздел не подходит — верни пустой массив.

Мероприятие:
{text}

Ответь ТОЛЬКО валидным JSON без пояснений: {{"category_l1_arr": ["раздел"]}}"""


def _pick_l1(post_text: str, data: dict, valid_l1: list):
    """Returns a list of sections (may be empty) or None if no model answered."""
    title = str(data.get('title') or '').strip()
    desc = str(data.get('description') or '').strip()
    text = f"{title}\n{desc}".strip() if desc else f"{title}\n{(post_text or '')[:1500]}".strip()
    prompt = PICK_L1_PROMPT.format(l1_list=' / '.join(valid_l1), text=text[:2000])
    last_rate_err = None
    for attempt in (1, 2):
        rate_limited = False
        for provider, client, model in ANALYZE_CHAIN:
            if client is None:
                continue
            try:
                response = _chat(provider, client, model, prompt, max_tokens=400, temperature=0.0, thinking=False)
                raw = _strip_fence(response.choices[0].message.content)
                if not raw:
                    continue
                ans = json.loads(raw)
                if isinstance(ans, dict):
                    ans = ans.get('category_l1_arr', [])
                fitted, _ = _fit_l1(ans, valid_l1)
                return fitted[:2]
            except json.JSONDecodeError as e:
                logging.warning(f"pick_l1 JSON error {provider}/{model}: {e}")
                continue
            except Exception as e:
                if _is_rate_limit(e):
                    rate_limited = True
                    last_rate_err = e
                    continue
                logging.warning(f"pick_l1 error {provider}/{model}: {e}")
                continue
        if attempt == 1 and rate_limited:
            time.sleep(_retry_after(last_rate_err))
            continue
        break
    return None

PROMPT_TEMPLATE = """Проанализируй пост из Telegram-канала. Верни ТОЛЬКО валидный JSON без markdown, без пояснений.
Дата публикации поста: {post_date}
Год этого поста: {post_year} — если в тексте события год не указан явно, используй именно {post_year} (кроме случая "неизвестен" — см. правило ниже)
Пост:
{text}
Доступные категории:
{l2_json}
JSON-структура:
{{
  "is_event": true/false,
  "title": "информативное название мероприятия (минимум 5-6 слов, включая тип события). Например: 'Выставка современного искусства «Выбирай»', Не используй просто название без типа события. null если неизвестно",
  "date": "YYYY-MM-DD или null",
  "time": "HH:MM или null",
  "is_free": true/false/null,
  "price": "минимальная цена ТОЛЬКО цифрами или null",
  "for_children": true/false/null,
  "format": "offline/online/unknown",
  "location": "название места проведения или null",
  "city_parsed": ["город1", "город2"] или [],
  "address": "адрес или метро или null",
  "category_l1_arr": ["1-3 категории из: {l1_list}"],
  "category_l2_arr": ["ТОЛЬКО 1-2 наиболее точные подкатегории СТРОГО из {l2_json}. Если подходит только одна — верни одну. Не перечисляй все возможные. Пустой массив [] если нет подходящих"],
  "description": "подробное описание до 500 символов или null"
}}
Правила:
- is_event = true только если это анонс конкретного мероприятия с датой или временем
- is_event = false если пост про скидки, акции, распродажи, поступление товара, новинки
- Если дата не указана явно в тексте — date = null
- is_event = false если пост содержит несколько разных мероприятий без конкретного описания одного события
- Дата публикации поста нужна ТОЛЬКО чтобы взять из неё год (см. "Год этого поста" выше), если в тексте есть день и месяц события, но нет года. НИКОГДА не используй дату публикации целиком как дату события. Если в тексте нет ни дня, ни месяца события — date = null, даже если известна дата публикации
- НИКОГДА не подставляй год по своим предположениям о текущей дате — единственный источник года по умолчанию это "Год этого поста" выше
- Если "Год этого поста" = "неизвестен" и в тексте события год явно не указан — date = null (не подставляй никакой год наугад)
- Если мероприятие идёт несколько дней или указано несколько дат ("29 и 30 августа", "с 5 по 8 сентября", "каждые выходные до 10 октября") — date = САМЫЙ РАННИЙ день из перечисленных. Никогда не подставляй вместо него дату публикации поста
- format = offline если есть адрес/место, online если zoom/трансляция/онлайн, unknown если непонятно
- for_children = true если мероприятие ТОЛЬКО для детей (утренник, детский праздник, развивашки, детский мастер-класс) или явно написано, что взрослым нельзя / только для детей
- for_children = null (семейное) ТОЛЬКО если явно сказано, что подходит и детям и взрослым ("для всей семьи", "можно с детьми", "семейный"), или это семейный формат: парк, фестиваль, квест для всей семьи
- for_children = false если явно написано, что детям нельзя (18+, "только для взрослых"), ИЛИ это лекция, концерт, вечеринка, стендап, бизнес, нетворкинг, психология, знакомства, дегустация, корпоратив и дети в посте не упоминаются
- Если сомневаешься и дети в посте не упоминаются — for_children = false
- is_free = true ТОЛЬКО если бесплатен вход или участие в самом мероприятии для всех: "бесплатно", "free", "вход свободный", "вход свободен", "0 руб", "без оплаты"
- Бесплатно С УСЛОВИЕМ — это НЕ is_free = true: "бесплатно для новых клиентов", "бесплатно первым 10", "по промокоду", "при покупке", "первое/пробное занятие бесплатно", "бесплатно для подписчиков / участников клуба / резидентов". Если цена при этом не указана — is_free = null; если указана — is_free = false и price
- Слово "бесплатно" про подарок, консультацию, разбор, диагностику, дегустацию, бонус, раздатку — не делает само мероприятие бесплатным: решай по цене входа
- is_free = false если указана любая цена (кроме 0), есть "купить билет", "билеты от", "стоимость", ссылка на продажу билетов (timepad/kassir/afisha/qtickets/ticketscloud/radario)
- Одна только "регистрация" или "запись" (ссылка, бот, телефон) без цены и без слов о бесплатности — is_free = null: у бесплатных мероприятий тоже бывает регистрация
- is_free = null если нет никаких признаков цены
- price = минимальная цена ТОЛЬКО цифрами, без слов и валюты, например "1500" (НЕ "от 1500 рублей", НЕ "1500 руб", НЕ "от 1500"). null если бесплатно или неизвестно
- location = название клуба/кафе/площадки. MAX, Telegram, WhatsApp, VK — это мессенджеры/соцсети, не место проведения, location = null
- address = улица/метро если указаны
- city_parsed = название города на русском языке (не "Moscow" — "Москва", "Мск" или "мск" — "Москва", "СПб", "Питер", "спб" — "Санкт-Петербург")
- city_parsed = массив городов проведения на русском, без районов ([] если не указан город)
- category_l1_arr и category_l2_arr — ТОЛЬКО значения из предоставленного списка
- is_event = false если пост описывает прошедшее мероприятие
- is_event = false если пост является расписанием, заголовком, афишей без конкретного описания мероприятия
- title должен быть информативным: содержать тип события (выставка, концерт, спектакль, лекция, мастер-класс и т.д.) + название. Минимум 5 слов. Не копируй заголовок поста дословно если он неинформативен"""

SCREEN_PROMPT = """Определи: является ли этот пост анонсом конкретного предстоящего мероприятия с датой?

Пост:
{text}

Не является мероприятием:
- скидки, акции, распродажи, поступление товара, новинки
- прошедшие события
- расписание или афиша без описания конкретного события
- пост содержит несколько разных мероприятий без описания одного
- нет конкретной даты проведения

Ответь ТОЛЬКО валидным JSON без пояснений:
{{"is_event": true}} или {{"is_event": false}}"""

async def screen_post(text: str) -> tuple[bool, str | None]:
    """Возвращает (это событие?, какая модель ответила).
    Вторым значением None — если не ответила ни одна модель."""
    if not text or len(text.strip()) < 20:
        return False, 'local/too-short'
    prompt = SCREEN_PROMPT.format(text=text[:2000])

    last_rate_err = None
    for attempt in (1, 2):
        rate_limited = False
        for provider, client, model in SCREEN_CHAIN:
            if client is None:
                continue
            try:
                response = _chat(provider, client, model, prompt,
                                 max_tokens=20, temperature=0.0, thinking=False)
                raw = (response.choices[0].message.content or '').strip()
                if raw.startswith('```'):
                    raw = raw.split('```')[1]
                    if raw.startswith('json'):
                        raw = raw[4:]
                data = json.loads(raw.strip())
                return bool(data.get('is_event', True)), f"{provider}/{model}"
            except json.JSONDecodeError as e:
                logging.warning(f"screen_post JSON error {provider}/{model}: {e}")
                continue
            except Exception as e:
                if _is_rate_limit(e):
                    rate_limited = True
                    last_rate_err = e
                    logging.warning(f"screen_post rate limit {provider}/{model} — следующая модель")
                    continue
                logging.warning(f"screen_post error {provider}/{model}: {e}")
                continue

        if attempt == 1 and rate_limited:
            wait = _retry_after(last_rate_err)
            logging.warning(f"screen_post: все модели по лимиту, пауза {wait}s и один повтор")
            time.sleep(wait)
            continue
        break

    # Ни одна модель не ответила — пропускаем пост дальше, на вторую линию.
    return True, None


async def analyze_post(text: str, post_date: str = '', categories: dict = {}) -> dict | None:
    if not text or len(text.strip()) < 20:
        return None

    l1_list = "/".join(categories.keys()) if categories else "Культура и искусство/Музыка/Кино/Театр и шоу/Выставки/Спорт и активности/Образование и лекции/Игры/Вечеринки и тусовки/Фестивали/Для детей/Знакомства/Психология и практики/Бизнес и нетворкинг/Творчество/Онлайн"
    l2_json = json.dumps(categories, ensure_ascii=False) if categories else "{}"

    post_year = post_date[:4] if post_date and len(post_date) >= 4 and post_date[:4].isdigit() else 'неизвестен'

    prompt = PROMPT_TEMPLATE.format(
        text=text[:3000],
        post_date=post_date,
        post_year=post_year,
        l1_list=l1_list,
        l2_json=l2_json
    )

    max_tokens = 6000 if ANALYZE_THINKING else 2000
    last_rate_err = None

    for attempt in (1, 2):
        rate_limited = False
        for provider, client, model in ANALYZE_CHAIN:
            if client is None:
                continue
            model_used = f"{provider}/{model}"
            try:
                response = _chat(provider, client, model, prompt,
                                 max_tokens=max_tokens, temperature=0.1,
                                 thinking=ANALYZE_THINKING)
                raw = response.choices[0].message.content
                if not raw:
                    continue
                raw = raw.strip()

                if raw.startswith('```'):
                    raw = raw.split('```')[1]
                    if raw.startswith('json'):
                        raw = raw[4:]

                data = json.loads(raw.strip())
                if isinstance(data, list):
                    data = data[0] if data else None
                if data and isinstance(data.get('category_l2_arr'), list):
                    data['category_l2_arr'] = [x.lower() for x in data['category_l2_arr']]
                if data and categories:
                    valid_l2 = [l2.lower() for l1_vals in categories.values() for l2 in l1_vals]
                    data['category_l2_arr'] = [x for x in data.get('category_l2_arr', []) if x in valid_l2]
                if data and categories:
                    # patch 2, 3 (v45): fit to the справочник; nothing fitted for an event -> the AI picks by meaning
                    valid_l1 = list(categories.keys())
                    fitted, left = _fit_l1(data.get('category_l1_arr'), valid_l1)
                    data['category_l1_arr'] = fitted
                    if not fitted and data.get('is_event'):
                        picked = _pick_l1(text, data, valid_l1)
                        if picked is None:
                            logging.warning(f"{model_used}: категория {left} не из справочника, повторный запрос не ответил — пост повторится в следующем прогоне")
                            return None
                        data['category_l1_arr'] = picked
                        logging.info(f"Категория выбрана повторным запросом: {left} → {picked}")
                if data and not data.get('category_l1_arr'):
                    data['is_event'] = False
                if data:
                    data['model'] = model_used
                logging.info(f"{model_used}: {data}")
                return data

            except json.JSONDecodeError as e:
                logging.warning(f"JSON parse error {model_used}: {e}")
                continue
            except Exception as e:
                if _is_rate_limit(e):
                    rate_limited = True
                    last_rate_err = e
                    logging.warning(f"rate limit {model_used} — следующая модель")
                    continue
                logging.error(f"API error {model_used}: {e}")
                log_parse(
                    channel='',
                    post_id='',
                    model=model_used,
                    fallback=provider != 'zai',
                    success=False,
                    error=str(e)[:500]
                )
                continue

        if attempt == 1 and rate_limited:
            wait = _retry_after(last_rate_err)
            logging.warning(f"analyze_post: все модели по лимиту, пауза {wait}s и один повтор")
            time.sleep(wait)
            continue
        break

    if last_rate_err is not None:
        log_parse(
            channel='',
            post_id='',
            model='all',
            fallback=True,
            success=False,
            error=f"rate limit на всех моделях: {str(last_rate_err)[:400]}"
        )
    return None
