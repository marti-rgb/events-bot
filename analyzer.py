import os
import json
import logging
from database import log_parse
from openai import OpenAI

cerebras_client = OpenAI(
    base_url="https://api.cerebras.ai/v1",
    api_key=os.getenv('CEREBRAS_API_KEY')
)

groq_client = None
try:
    from groq import Groq
    groq_client = Groq(api_key=os.getenv('GROQ_API_KEY'))
except:
    pass

PROMPT_TEMPLATE = """Проанализируй пост из Telegram-канала. Верни ТОЛЬКО валидный JSON без markdown, без пояснений.
Дата публикации поста: {post_date}
Пост:
{text}
Доступные категории:
{l2_json}
JSON-структура:
{{
  "is_event": true/false,
  "title": "название мероприятия или null",
  "date": "YYYY-MM-DD или null",
  "time": "HH:MM или null",
  "is_free": true/false/null,
  "price": "стоимость текстом или null",
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
- Если год не указан — используй год из даты публикации поста
- format = offline если есть адрес/место, online если zoom/трансляция/онлайн, unknown если непонятно
- for_children = true если явно для детей или семей с детьми
- is_free = true если есть: "бесплатно", "free", "вход свободный", "0 руб", "без оплаты", "вход свободен"
- is_free = false если: указана любая цена, есть "купить билет", "билеты", "регистрация", "tickets", ссылка на timepad/kassir/afisha
- is_free = null только если нет никаких признаков цены
- price = минимальная цена числом (только цифры), null если бесплатно или неизвестно
- price = минимальная цена числом (только цифры, без знаков и слов), null если неизвестно или бесплатно
- location = название клуба/кафе/площадки. MAX, Telegram, WhatsApp, VK — это мессенджеры/соцсети, не место проведения, location = null
- address = улица/метро если указаны
- city_parsed = название города на русском языке (не "Moscow" — "Москва", "Мск" или "мск" — "Москва", "СПб", "Питер", "спб" — "Санкт-Петербург")
- city_parsed = массив городов проведения на русском, без районов ([] если не указан город)
- category_l1_arr и category_l2_arr — ТОЛЬКО значения из предоставленного списка
- is_event = false если пост описывает прошедшее мероприятие
- is_event = false если пост является расписанием, заголовком, афишей без конкретного описания мероприятия"""

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

    for attempt, (client, model) in enumerate([
        (cerebras_client, "gpt-oss-120b"),
        (groq_client, "llama-3.3-70b-versatile"),
        (cerebras_client, "zai-glm-4.7"),
        (groq_client, "llama-3.1-8b-instant"),
    ]):        
        if client is None:
            continue
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=2000,
            )
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
                valid_l2 = [l2.lower() for l1_list in categories.values() for l2 in l1_list]
                data['category_l2_arr'] = [x for x in data.get('category_l2_arr', []) if x in valid_l2]
            if data and categories:
                valid_l1 = list(categories.keys())
                data['category_l1_arr'] = [x for x in data.get('category_l1_arr', []) if x in valid_l1]
            if data and not data.get('category_l1_arr'):
                data['is_event'] = False
            model_used = f"{'cerebras' if attempt == 0 else 'groq'}/{model}"
            if data:
                data['model'] = model_used
            logging.info(f"{model_used}: {data}")
            return data
            
        except json.JSONDecodeError as e:
            logging.warning(f"JSON parse error: {e}")
            continue
        except Exception as e:
            provider = 'cerebras' if client == cerebras_client else 'groq'
            model_used = f"{provider}/{model}"
            logging.error(f"API error {model_used}: {e}")
            log_parse(
                channel='',
                post_id='',
                model=model_used,
                fallback=provider == 'groq',
                success=False,
                error=str(e)[:500]
            )
            if '429' in str(e):
                import time
                time.sleep(13)
            continue
    return None
