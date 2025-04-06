import os
import asyncio
from flask import Flask, request, Response, stream_with_context, jsonify
from flask_cors import CORS
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import json
import re
from threading import Lock
from collections import Counter

app = Flask(__name__)
CORS(app, origins=["https://metrics.tilda.ws"])  # Разрешаем только твой сайт

search_lock = Lock()
stop_flag = False

top_regions = Counter()
top_districts = Counter()
top_settlements = Counter()
top_surnames = Counter()

invalid_birthplace_patterns = [
    r'^красноармеец', r'^сержант', r'^курсант', r'^б/зв', r'^ефрейтор', r'^рядовой', r'^старшина',
    r'^мл\.?\sсержант', r'^ст\.?\sсержант', r'^старший\sлейтенант', r'^лейтенант', r'^капитан', r'^подполковник',
    r'\. ВПП', r'\. ЗП', r'\. Картотека', r'\. Призыв', r'\. Демобилизация', r'\. УПК',
    r'\. Юбилейная картотека', r'\. Пленение', r'\. Картотека ранений',
    r'^\.\s', r'^-\d{2}-\d{2}'
]

def is_valid_birthplace(place):
    place = place.lower().strip()
    for pattern in invalid_birthplace_patterns:
        if re.search(pattern, place):
            return False
    return True

def parse_birthplace(place):
    region, district, settlement = None, None, None
    parts = [p.strip() for p in place.split(',')]
    for part in parts:
        if any(word in part for word in ['обл', 'край', 'АССР']):
            region = part
        elif any(word in part for word in ['р-н', 'г.']):
            district = part
        elif any(word in part for word in ['с.', 'д.', 'с/с', 'с/з']):
            settlement = part
    return region, district, settlement

def update_counters(hero, mode):
    if mode == 'surname':
        region, district, settlement = parse_birthplace(hero["место рождения"])
        if region:
            top_regions[region] += 1
        if district:
            top_districts[district] += 1
        if settlement:
            top_settlements[settlement] += 1
    elif mode == 'birthplace':
        surname = hero["фамилия"]
        if surname:
            top_surnames[surname] += 1

async def fetch_heroes(surname, place_birth, max_pages, mode):
    global stop_flag
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        page_num = 1
        empty_records = 0

        while page_num <= max_pages and empty_records < 20:
            if stop_flag:
                break

            if mode == 'surname':
                search_url = f"https://pamyat-naroda.ru/heroes/?last_name={surname}&page={page_num}&grouppersons=1"
                if place_birth:
                    search_url += f"&place_birth={place_birth}"
            else:
                search_url = f"https://pamyat-naroda.ru/heroes/?page={page_num}&grouppersons=1&place_birth={place_birth}"

            await page.goto(search_url)
            await page.wait_for_timeout(1000)

            content = await page.content()
            soup = BeautifulSoup(content, 'html.parser')
            items = soup.select('.heroes-list-item')

            if not items:
                empty_records += 1
                page_num += 1
                continue

            matched = False

            for item in items:
                if stop_flag:
                    break

                name_elem = item.select_one('.heroes-list-item-name')
                info_elem = item.select_one('.heroes-list-item-info')

                if name_elem and info_elem:
                    name_text = name_elem.text.strip()
                    info_text = info_elem.text.strip()

                    name_parts = name_text.split()
                    if len(name_parts) == 0:
                        continue

                    year_match = re.search(r'(\d{4})', info_text)
                    year = year_match.group(1) if year_match else ""

                    place = info_text
                    if year:
                        place = place.split(year, 1)[-1]
                        place = place.strip(' ,')

                    if "Место службы" in place:
                        place = place.split("Место службы")[0].strip(' ,')

                    if not place or len(place) < 3 or not is_valid_birthplace(place):
                        continue

                    hero = {
                        "фамилия": name_parts[0],
                        "имя": name_parts[1] if len(name_parts) > 1 else "",
                        "отчество": name_parts[2] if len(name_parts) > 2 else "",
                        "год рождения": year,
                        "место рождения": place,
                        "url": name_elem.get('href') if name_elem.get('href', '').startswith('http') else "https://pamyat-naroda.ru" + name_elem.get('href')
                    }

                    update_counters(hero, mode)
                    yield hero
                    matched = True

            if not matched:
                empty_records += 1
            else:
                empty_records = 0

            yield f"event: page\ndata: {page_num}\n\n"
            page_num += 1

        await browser.close()

async def async_gen_rows(surname, place_birth, max_pages, mode):
    async for hero in fetch_heroes(surname, place_birth, max_pages, mode):
        if isinstance(hero, dict):
            yield f"data: {json.dumps(hero, ensure_ascii=False)}\n\n"
        else:
            yield hero
    yield "data: [END]\n\n"

def generate_rows(surname, place_birth, max_pages, mode):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async_gen = async_gen_rows(surname, place_birth, max_pages, mode)

    async def iterate_gen():
        async for row in async_gen:
            yield row

    gen = iterate_gen()
    try:
        while True:
            row = loop.run_until_complete(gen.__anext__())
            yield row
    except StopAsyncIteration:
        pass
    finally:
        loop.close()

@app.route('/stream')
def stream():
    global stop_flag, top_regions, top_districts, top_settlements, top_surnames
    stop_flag = False
    top_regions.clear()
    top_districts.clear()
    top_settlements.clear()
    top_surnames.clear()

    surname = request.args.get('surname', '')
    place_birth = request.args.get('place_birth', '')
    max_pages = int(request.args.get('max_pages', 50))
    mode = request.args.get('mode', 'surname')

    if not surname and not place_birth:
        return "Missing parameters", 400

    if not search_lock.acquire(blocking=False):
        return "Search already in progress", 429

    def release_lock_after_search():
        try:
            yield from generate_rows(surname, place_birth, max_pages, mode)
        finally:
            search_lock.release()

    return Response(stream_with_context(release_lock_after_search()), mimetype='text/event-stream')

@app.route('/stop', methods=['POST'])
def stop_search():
    global stop_flag
    stop_flag = True
    return "OK"

@app.route('/chart')
def get_chart_data():
    mode = request.args.get('mode', 'surname')
    chart_type = request.args.get('type', '')

    if mode == 'surname':
        if chart_type == 'regions':
            counter = top_regions
        elif chart_type == 'districts':
            counter = top_districts
        elif chart_type == 'settlements':
            counter = top_settlements
        else:
            return jsonify({'labels': [], 'data': []})
    elif mode == 'birthplace':
        counter = top_surnames
    else:
        return jsonify({'labels': [], 'data': []})

    top_items = counter.most_common(10)
    labels, data = zip(*top_items) if top_items else ([], [])
    return jsonify({'labels': labels, 'data': data})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
