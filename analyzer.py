import os
import json
import logging
from groq import Groq

client = Groq(api_key=os.getenv('GROQ_API_KEY'))

PROMPT_TEMPLATE = """Проанализируй пост из Telegram-канала. Верни ТОЛЬКО валидный JSON без markdown, без пояснений.
Пост:
{text}
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
  "category": "одно из: настолки/культура/спорт/образование/музыка/кино/театр/выставка/лекция/мастер-класс/вечеринка/другое",
  "description": "подробное описание до 500 символов или null"
}}
Правила:
- is_event = true только если это анонс конкретного мероприятия с датой или временем
- Если дата не указана явно в тексте — date = null
- format = offline если есть адрес/место, online если zoom/трансляция/онлайн, unknown если непонятно
- for_children = true если явно для детей или семей с детьми
- price = сумма в рублях текстом если указана, null если неизвестно
- location = название клуба/кафе/площадки
- address = улица/метро если указаны"""

async def analyze_post(text: str) -> dict | None:
    if not text or len(text.strip()) < 20:
        return None
    
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "user", "content": PROMPT_TEMPLATE.format(text=text[:3000])}
            ],
            temperature=0.1,
            max_tokens=800,
        )
        raw = response.choices[0].message.content.strip()
        
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
        
        data = json.loads(raw.strip())
        logging.info(f"Groq: {data}")
        return data
    except json.JSONDecodeError as e:
        logging.warning(f"JSON parse error: {e}")
        return None
    except Exception as e:
        logging.error(f"Groq API error: {e}")
        return None
