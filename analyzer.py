import os
import json
import logging
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
  "for_children": true/false,
  "format": "offline/online/unknown",
  "location": "название места проведения или null",
  "address": "адрес или метро или null",
  "category_l1_arr": ["до 3 категорий из: {l1_list}"],
  "category_l2_arr": ["до 3 подкатегорий ТОЛЬКО из списка выше, пустой массив [] если нет подходящих"],
  "description": "подробное описание до 500 символов или null"
}}
Правила:
- is_event = true только если это анонс конкретного мероприятия с датой или временем
- is_event = false если пост про скидки, акции, распродажи, поступление товара, новинки
- Если дата не указана явно в тексте — date = null
- Если год не указан — используй год из даты публикации поста
- format = offline если есть адрес/место, online если zoom/трансляция/онлайн, unknown если непонятно
- for_children = true если явно для детей или семей с детьми
- price = минимальная цена числом (только цифры, без знаков и слов), null если неизвестно или бесплатно
- location = название клуба/кафе/площадки
- address = улица/метро если указаны
- category_l1_arr и category_l2_arr — ТОЛЬКО значения из предоставленного списка"""

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
        (cerebras_client, "llama-3.1-8b"),
        (groq_client, "llama-3.3-70b-versatile")
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
            raw = response.choices[0].message.content.strip()

            if raw.startswith('```'):
                raw = raw.split('```')[1]
                if raw.startswith('json'):
                    raw = raw[4:]

            data = json.loads(raw.strip())
            if isinstance(data, list):
                data = data[0] if data else None
            logging.info(f"{'Cerebras' if attempt == 0 else 'Groq'}: {data}")
            return data
            
        except json.JSONDecodeError as e:
            logging.warning(f"JSON parse error: {e}")
            continue
        except Exception as e:
            logging.error(f"API error ({'Cerebras' if attempt == 0 else 'Groq'}): {e}")
            continue

    return None
