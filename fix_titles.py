import os
import json
import logging
import requests
from openai import OpenAI
from datetime import date

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
CEREBRAS_API_KEY = os.getenv('CEREBRAS_API_KEY')

cerebras_client = OpenAI(
    base_url="https://api.cerebras.ai/v1",
    api_key=CEREBRAS_API_KEY
)

HEADERS = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json'
}

TITLE_PROMPT = """Ты помощник для улучшения названий мероприятий.

Исходное название: {title}
Описание: {description}

Сформулируй информативное название мероприятия:
- Минимум 5-6 слов
- Включи тип события (выставка, концерт, спектакль, лекция, мастер-класс, фестиваль, вечеринка и т.д.)
- Сохрани оригинальное название если оно есть
- Примеры: "Выставка современного искусства «Выбирай»", "Концерт группы Wildways", "Спектакль «Безумный Пьеро» в Центре Зотов"

Верни ТОЛЬКО новое название без кавычек, пояснений и markdown."""


def get_events():
    today = date.today().isoformat()
    url = f"{SUPABASE_URL}/rest/v1/events"
    params = {
        'select': 'id,title,description',
        'date': f'gte.{today}',
        'title_original': 'is.null',
        'order': 'date.asc'
    }
    res = requests.get(url, headers=HEADERS, params=params)
    events = res.json()
    
    # Фильтруем только короткие title (меньше 5 слов)
    short = [e for e in events if e.get('title') and len(e['title'].split()) < 5]
    logging.info(f"Найдено событий с коротким title: {len(short)} из {len(events)} актуальных")
    return short


def fix_title(title, description):
    prompt = TITLE_PROMPT.format(
        title=title or '',
        description=(description or '')[:500]
    )
    
    models = ['zai-glm-4.7', 'gpt-oss-120b']
    
    for model in models:
        try:
            response = cerebras_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=100
            )
            logging.info(f"[{model}] raw response: {response}")
            content = response.choices[0].message.content
            logging.info(f"[{model}] content: {repr(content)}")
            if content and content.strip() and len(content.strip()) > 5:
                result = content.strip()
                logging.info(f"[{model}] '{title}' → '{result}'")
                return result
            else:
                logging.warning(f"[{model}] пустой или короткий ответ: {repr(content)}")
        except Exception as e:
            logging.warning(f"[{model}] ошибка: {e}")
            continue
    
    return None


def update_event(event_id, new_title, old_title):
    url = f"{SUPABASE_URL}/rest/v1/events"
    params = {'id': f'eq.{event_id}'}
    data = {
        'title': new_title,
        'title_original': old_title
    }
    res = requests.patch(url, headers=HEADERS, params=params, json=data)
    return res.status_code == 204


def main():
    events = get_events()
    if not events:
        logging.info("Нет событий для обработки")
        return

    success = 0
    failed = 0

    # for i, event in enumerate(events):
    for i, event in enumerate(events[:5]):
        event_id = event['id']
        old_title = event['title']
        description = event.get('description', '')

        logging.info(f"[{i+1}/{len(events)}] Обрабатываю: {old_title}")

        new_title = fix_title(old_title, description)
        
        if new_title and new_title != old_title:
            if update_event(event_id, new_title, old_title):
                success += 1
                logging.info(f"✅ Обновлено: '{old_title}' → '{new_title}'")
            else:
                failed += 1
                logging.error(f"❌ Ошибка обновления id={event_id}")
        else:
            logging.info(f"⏭ Пропущено (нет улучшения): {old_title}")

    logging.info(f"\nГотово. Обновлено: {success}, ошибок: {failed}, пропущено: {len(events) - success - failed}")


if __name__ == '__main__':
    main()
