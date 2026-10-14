import pdfplumber, requests, json

# 1. Извлечь текст
with pdfplumber.open("resume_withour_certificate.pdf") as pdf:
    text = "\n".join(p.extract_text() or "" for p in pdf.pages)

# 2. Отправить в Ollama
prompt = f"""Извлеки данные моряка. Верни строго JSON по схеме:
{{
  "full_name": null,
  "date_of_birth": null,
  "citizenship": null,
  "email": null,
  "phone": null,
  "rank": null,
  "vessel_types": [],
  "trading_area": null,
  "years_in_rank": null,
  "total_sea_years": null,
  "sea_service": [
    {{"period": null, "rank": null, "vessel": null, "vessel_type": null, "dwt": null}}
  ],
  "certificates": [
    {{"name": null, "number": null, "issued_date": null, "expiry_date": null}}
  ],
  "medical_valid_until": null,
  "seamans_book": {{"number": null, "valid_until": null}},
  "languages": [{{"language": null, "level": null}}],
  "education": {{"institution": null, "specialty": null, "year": null}}
}}

ПРАВИЛА ПО ДАТАМ (ОЧЕНЬ ВАЖНО):
- "expiry_date" — это ДАТА ОКОНЧАНИЯ срока действия. Ищи в тексте после слов:
  "valid until", "годен до", "действует до", "действителен до",
  "expires", "expiry", "срок действия до", "до" (в контексте сертификата),
  "valid through", "срок годности до".
- "issued_date" — это ДАТА ВЫДАЧИ. Ищи после слов:
  "issued", "выдан", "дата выдачи", "issued on".
- Если у сертификата указана только дата "до ..." — это expiry_date, issued_date = null.
- Если указана только дата выдачи без срока — issued_date заполняй, expiry_date = null.
- Формат даты: сохраняй как в тексте (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД).
- Если дата не указана вообще — null. НЕ придумывай и НЕ переноси дату из другого сертификата.

ПРАВИЛА ПО СЕРТИФИКАТАМ:
- В "certificates" только профессиональные сертификаты (STCW, GMDSS, AFF, MFA, Radar и т.п.).
- Медкомиссию клади в "medical_valid_until".
- УЛМ клади в "seamans_book".
- Языки клади в "languages".
- Образование клади в "education".
- Поле "name" сертификата — только название, без номера и дат.
- Поле "number" — только номер, без названия и дат.

Текст:
{text}
"""

r = requests.post("http://localhost:11434/api/generate", json={
    "model": "qwen2.5:0.5b",
    "prompt": prompt,
    "stream": False,
    "format": "json",              # ← заставит вернуть валидный JSON
    "options": {"temperature": 0}
})
print(r.json()["response"])
