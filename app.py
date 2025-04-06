# Установка браузеров Playwright при старте приложения
import subprocess
subprocess.run(["playwright", "install", "chromium"], check=True)

import asyncio
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import json
import urllib.parse

# === Flask-приложение ===
app = Flask(__name__)
CORS(app)

# === Глобальные переменные ===
searching = False
chart_data = {
    "surname": {
        "regions": {},
        "districts": {},
        "settlements": {}
    },
    "birthplace": {
        "surnames": {}
    }
}

# === Очистка мусорных фраз ===
def clean_text(text):
    garbage_phrases = [
        "Донесение о потерях", "Книга Памяти", "Данные об утрате документов",
        "Донесение о безвозвратных потерях", "данные ОБД", "данные картотеки",
        "Юбилейная картотека", "Донесения о потерях", "Картотека потерь",
        "Печатная Книга Памяти", "Уточнение потерь", "Картотека захоронений"
    ]
    for phrase in garbage_phrases:
        if phrase.lower() in text.lower():
            return ''
    return text

# === Генерация URL для поиска ===
def build_search_url(surname, place_birth, page, mode):
    base = "https://pamyat-naroda.ru/heroes/"
    params = {
        "group": "all",
        "types": ":".join([
            "pamyat_commander", "nagrady_nagrad_doc", "nagrady_uchet_kartoteka",
            "nagrady_ubilein_kartoteka", "pdv_kart_in", "pdv_kart_in_inostranec",
            "pamyat_voenkomat", "potery_vpp", "pamyat_zsp_parts", "kld_ran",
            "kld_bolezn", "kld_card", "kld_upk", "kld_vmf", "kld_partizan",
            "potery_doneseniya_o_poteryah", "potery_gospitali", "potery_utochenie_poter",
            "potery_spiski_zahoroneniy", "potery_voennoplen", "potery_iskluchenie_iz_spiskov",
            "potery_kartoteki", "potery_rvk_extra", "potery_isp_extra",
            "same_doroga", "same_rvk", "same_guk", "potery_knigi_pamyati"
        ]),
        "page": page,
        "grouppersons": "1"
    }
    if mode == "surname":
        params["last_name"] = surname
        if place_birth:
            params["place_birth"] = place_birth
    elif mode == "birthplace":
        params["place_birth"] = place_birth
    return base + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

# === Основной асинхронный парсер ===
async def fetch_heroes(surname, place_birth, max_pages, mode):
    global searching
    searching = True
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-zygote",
                "--single-process"
            ]
        )
        page = await browser.new_page()
        no_result_count = 0

        for current_page in range(1, max_pages + 1):
            if not searching:
                break

            url = build_search_url(surname, place_birth, current_page, mode)
            await page.goto(url)
            await asyncio.sleep(1)

            content = await page.content()
            soup = BeautifulSoup(content, "html.parser")
            records = soup.select('.card-person')

            if not records:
                no_result_count += 1
                if no_result_count >= 20:
                    break
                continue

            no_result_count = 0

            for record in records:
                try:
                    fullname = record.select_one(".card-person-title__name").text.strip().split()
                    last_name, first_name, middle_name = (fullname + ["", ""])[:3]
                    birth_year = record.select_one(".card-person-info__item--year .card-person-info__value")
                    birth_year = birth_year.text.strip() if birth_year else ""

                    place = record.select_one(".card-person-info__item--birthPlace .card-person-info__value")
                    place = place.text.strip() if place else ""
                    place = clean_text(place)

                    link = record.select_one(".card-person-title__name a")
                    url = "https://pamyat-naroda.ru" + link['href'] if link else ""

                    if mode == "surname":
                        if place_birth and place_birth.lower() not in place.lower():
                            continue
                        region, district, settlement = (place.split(",") + ["", ""])[:3]
                        chart_data["surname"]["regions"][region.strip()] = chart_data["surname"]["regions"].get(region.strip(), 0) + 1
                        chart_data["surname"]["districts"][district.strip()] = chart_data["surname"]["districts"].get(district.strip(), 0) + 1
                        chart_data["surname"]["settlements"][settlement.strip()] = chart_data["surname"]["settlements"].get(settlement.strip(), 0) + 1
                    elif mode == "birthplace":
                        chart_data["birthplace"]["surnames"][last_name.strip()] = chart_data["birthplace"]["surnames"].get(last_name.strip(), 0) + 1

                    yield {
                        "фамилия": last_name,
                        "имя": first_name,
                        "отчество": middle_name,
                        "год рождения": birth_year,
                        "место рождения": place,
                        "url": url
                    }
                except Exception:
                    continue

            yield "page"

        await browser.close()
    searching = False

# === Генераторы данных ===
async def async_gen_rows(surname, place_birth, max_pages, mode):
    async for hero in fetch_heroes(surname, place_birth, max_pages, mode):
        yield hero

def generate_rows(surname, place_birth, max_pages, mode):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async_gen = async_gen_rows(surname, place_birth, max_pages, mode)

    async def iterate_gen():
        async for row in async_gen:
            yield row

    gen = iterate_gen()

    while True:
        try:
            row = loop.run_until_complete(gen.__anext__())
            if row == "page":
                yield 'event: page\ndata: page\n\n'
            else:
                yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
        except StopAsyncIteration:
            yield "data: [END]\n\n"
            break

# === Маршруты Flask ===
@app.route('/stream')
def stream():
    surname = request.args.get('surname', '').strip()
    place_birth = request.args.get('place_birth', '').strip()
    max_pages = int(request.args.get('max_pages', '50'))
    mode = request.args.get('mode', 'surname')
    return Response(generate_rows(surname, place_birth, max_pages, mode), mimetype='text/event-stream')

@app.route('/chart')
def chart():
    mode = request.args.get('mode', 'surname')
    if mode == "surname":
        chart_type = request.args.get('type')
        data = chart_data["surname"].get(chart_type, {})
    else:
        data = chart_data["birthplace"]["surnames"]
    sorted_items = sorted(data.items(), key=lambda x: x[1], reverse=True)[:10]
    labels, counts = zip(*sorted_items) if sorted_items else ([], [])
    return jsonify({"labels": labels, "data": counts})

@app.route('/stop', methods=['POST'])
def stop():
    global searching
    searching = False
    return '', 204

@app.route('/')
def home():
    return 'OK'

# === Локальный запуск (если нужен) ===
if __name__ == '__main__':
    app.run(host="0.0.0.0", port=10000)
