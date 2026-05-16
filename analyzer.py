import os
import json
import logging
import google.generativeai as genai

genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
model = genai.GenerativeModel('gemini-2.0-flash')

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
  "for_children": true/false,
  "format": "offline/online/unknown",
  "category": "одно из: настолки/культура/спорт/образование/музыка/кино/театр/выставка/лекция/мастер-класс/вечеринка/другое",
  "description": "краткое описание до 200 символов или null"
}}

Правила:
- is_event = true только если это анонс конкретного мероприятия с датой или временем
- format = offline если есть адрес/место, online если zoom/трансляция/онлайн, unknown если непонятно
- for_children = true если явно для детей или семей с детьми
- Если дата не указана явно — date = null"""

async def analyze_post(text: str) -> dict | None:
    if not text or len(text.strip()) < 20:
        return None
    
    try:
        prompt = PROMPT_TEMPLATE.format(text=text[:3000])
        response = model.generate_content(prompt)
        raw = response.text.strip()
        
        # Убираем markdown если есть
        if raw.startswith('```'):
            raw = raw.split('```')[1]
            if raw.startswith('json'):
                raw = raw[4:]
        
        data = json.loads(raw.strip())
        return data
    except json.JSONDecodeError as e:
        logging.warning(f"JSON parse error: {e}")
        return None
    except Exception as e:
        logging.error(f"Gemini API error: {e}")
        return None
