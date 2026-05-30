import httpx
import logging

SPREADSHEET_ID = '1x8lh3JgDgR3hnhJH2N6DKbpGhN4MQv8QS6EOHPz9Ctk'

def get_sheet_csv(sheet_name: str) -> list[list[str]]:
    url = f'https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/gviz/tq?tqx=out:csv&sheet={sheet_name}'
    response = httpx.get(url, timeout=15)
    response.raise_for_status()
    lines = response.text.strip().splitlines()
    rows = []
    for line in lines:
        # Простой CSV парсинг (убираем кавычки)
        cols = [c.strip().strip('"') for c in line.split(',')]
        rows.append(cols)
    return rows

def load_channels() -> list[dict]:
    try:
        rows = get_sheet_csv('Каналы')
        if not rows:
            return []
        header = [h.lower() for h in rows[0]]
        channel_idx = header.index('channel')
        city_idx = header.index('город') if 'город' in header else None
        
        channels = []
        for row in rows[1:]:
            if len(row) <= channel_idx:
                continue
            channel = row[channel_idx].strip()
            if not channel:
                continue
            city = row[city_idx].strip() if city_idx and len(row) > city_idx else 'Москва'

            status_idx = header.index('статус') if 'статус' in header else None
            channels.append({
                'channel': channel,
                'city': city,
                'threads': None,
                'exclude_threads': [],
            })
        logging.info(f"Загружено каналов: {len(channels)}")
        return channels
    except Exception as e:
        logging.error(f"Ошибка загрузки каналов: {e}")
        return []

def load_keywords() -> list[str]:
    try:
        rows = get_sheet_csv('Ключевые слова')
        if not rows:
            return []
        keywords = [row[0].strip() for row in rows[1:] if row and row[0].strip()]
        logging.info(f"Загружено ключевых слов: {len(keywords)}")
        return keywords
    except Exception as e:
        logging.error(f"Ошибка загрузки ключевых слов: {e}")
        return []

UNAVAILABLE_CHANNELS = []
def load_categories() -> dict:
    try:
        rows = get_sheet_csv('Категории')
        if not rows:
            return {}
        header = [h.lower() for h in rows[0]]
        l1_idx = header.index('category_l1')
        l2_idx = header.index('category_l2')
        
        categories = {}
        for row in rows[1:]:
            if len(row) <= max(l1_idx, l2_idx):
                continue
            l1 = row[l1_idx].strip()
            l2 = row[l2_idx].strip()
            if not l1:
                continue
            if l1 not in categories:
                categories[l1] = []
            if l2:
                categories[l1].append(l2)
        
        logging.info(f"Загружено категорий l1: {len(categories)}")
        return categories
    except Exception as e:
        logging.error(f"Ошибка загрузки категорий: {e}")
        return {}

def match_category_l2(groq_l2: str, categories: dict, l1: str) -> str:
    if not groq_l2 or not l1 or l1 not in categories:
        return groq_l2 or 'другое'
    groq_lower = groq_l2.lower().strip()
    for l2 in categories[l1]:
        if l2.lower() in groq_lower or groq_lower in l2.lower():
            return l2
    return groq_l2

def load_stop_tags() -> list[str]:
    try:
        rows = get_sheet_csv('Стоп-теги')
        if not rows:
            return []
        tags = [row[0].strip().lower() for row in rows[1:] if row and row[0].strip()]
        logging.info(f"Загружено стоп-тегов: {len(tags)}")
        return tags
    except Exception as e:
        logging.error(f"Ошибка загрузки стоп-тегов: {e}")
        return []

