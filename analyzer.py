# ВЕРСИЯ v41 — 26.08.2026
# Изменено относительно v40: Cerebras отключён, первый провайдер Z.ai (glm-4.5-flash),
# запасные — Groq openai/gpt-oss-120b и openai/gpt-oss-20b; переключатель размышлений;
# 429 без длинной паузы; правки промпта (цена, дата, диапазон дат);
# честный лог первой линии; листание истории каналов; деление каналов на части;
# первая линия отключаема (USE_SCREEN).
# Переменные в GitHub → Settings → Variables:
#   ANALYZE_THINKING=0  PARSE_PAGES=1  USE_SCREEN=0  CHANNELS_PART=(пусто)
import os
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

PROMPT_TEMPLATE = """Проанализируй пост из Telegram-канала. Верни ТОЛЬКО валидный JSON без markdown, без пояснений.
Дата публикации поста: {post_date}
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
- Дата публикации поста нужна ТОЛЬКО чтобы взять из неё год, если в тексте есть день и месяц события, но нет года. НИКОГДА не используй дату публикации целиком как дату события. Если в тексте нет ни дня, ни месяца события — date = null, даже если известна дата публикации
- Если мероприятие идёт несколько дней или указано несколько дат ("29 и 30 августа", "с 5 по 8 сентября", "каждые выходные до 10 октября") — date = САМЫЙ РАННИЙ день из перечисленных. Никогда не подставляй вместо него дату публикации поста
- format = offline если есть адрес/место, online если zoom/трансляция/онлайн, unknown если непонятно
- for_children = true если мероприятие ТОЛЬКО для детей (утренник, детский праздник, развивашки)
- for_children = null если подходит и детям и взрослым (семейный формат, квест, фестиваль, парк)
- for_children = false если только для взрослых (18+, вечеринка, корпоратив)
- is_free = true если есть: "бесплатно", "free", "вход свободный", "0 руб", "без оплаты", "вход свободен"
- is_free = false если: указана любая цена, есть "купить билет", "билеты", "регистрация", "tickets", ссылка на timepad/kassir/afisha
- is_free = null только если нет никаких признаков цены
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

    prompt = PROMPT_TEMPLATE.format(
        text=text[:3000],
        post_date=post_date,
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
                    valid_l1 = list(categories.keys())
                    data['category_l1_arr'] = [x for x in data.get('category_l1_arr', []) if x in valid_l1]
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
