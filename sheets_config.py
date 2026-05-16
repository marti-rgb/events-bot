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
