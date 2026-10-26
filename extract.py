"""
extract.py — извлечение структурированных данных из резюме моряка.

Пайплайн:
1. PDF → текст (pdfplumber)
2. текст → JSON (Ollama + qwen2.5)
3. JSON → валидация (Pydantic)
4. проверка на галлюцинации (regex по датам)
5. красивый вывод
"""

import json
import re
import sys
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
from typing import Optional

import pdfplumber
import requests
from pydantic import BaseModel, Field, ValidationError


# ============================================================
# КОНФИГ
# ============================================================

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:3b"          # при плохом качестве заменить на qwen2.5:3b
TEMPERATURE = 0
TIMEOUT = 300                    # секунд


# ============================================================
# СХЕМА (Pydantic)
# ============================================================

class SeaService(BaseModel):
    period: Optional[str] = None
    rank: Optional[str] = None
    vessel: Optional[str] = None
    vessel_type: Optional[str] = None
    dwt: Optional[int] = None


class Certificate(BaseModel):
    name: Optional[str] = None
    number: Optional[str] = None
    issued_date: Optional[str] = None
    expiry_date: Optional[str] = None


class SeamansBook(BaseModel):
    number: Optional[str] = None
    valid_until: Optional[str] = None


class Language(BaseModel):
    language: Optional[str] = None
    level: Optional[str] = None


class Education(BaseModel):
    institution: Optional[str] = None
    specialty: Optional[str] = None
    year: Optional[int] = None


class Candidate(BaseModel):
    full_name: Optional[str] = None
    date_of_birth: Optional[str] = None
    citizenship: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    rank: Optional[str] = None
    vessel_types: list[str] = Field(default_factory=list)
    trading_area: Optional[str] = None
    years_in_rank: Optional[float] = None
    total_sea_years: Optional[float] = None
    sea_service: list[SeaService] = Field(default_factory=list)
    certificates: list[Certificate] = Field(default_factory=list)
    medical_valid_until: Optional[str] = None
    seamans_book: Optional[SeamansBook] = None
    languages: list[Language] = Field(default_factory=list)
    education: Optional[Education] = None

    @field_validator("vessel_types", mode="after")
    @classmethod
    def dedupe_vessel_types(cls, v: list[str]) -> list[str]:
        """Убирает дубликаты, сохраняя порядок. Регистронезависимо."""
        seen = set()
        result = []
        for item in v:
            key = item.strip().lower()
            if key and key not in seen:
                seen.add(key)
                result.append(item.strip())
        return result


# ============================================================
# ПРОМПТ
# ============================================================

PROMPT_TEMPLATE = """Извлеки данные моряка из текста. Верни СТРОГО JSON по схеме:
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
- "expiry_date" — ДАТА ОКОНЧАНИЯ срока действия. Ищи после слов:
  "valid until", "годен до", "действует до", "действителен до", "expires", "срок действия до", "до".
- "issued_date" — ДАТА ВЫДАЧИ. Ищи после слов: "issued", "выдан", "дата выдачи".
- ВСЕГДА приводи даты к формату ДД.ММ.ГГГГ (например, 15.01.2027).
- ЕСЛИ ДАТА НАПИСАНА СЛОВАМИ, переведи месяц в цифру.
  Пример: "15 января 2027" -> "15.01.2027".
  Пример: "May 2021" -> "01.05.2021".
- ЕСЛИ УКАЗАН ТОЛЬКО ГОД (например, "до 2029 года"), пиши "31.12.2029".
- Если срок действия "бессрочно" или "без даты", оставь expiry_date = null. НЕ ПРИДУМЫВАЙ ДАТУ.

ПРАВИЛА ПО СЕРТИФИКАТАМ:
- В "certificates" только профессиональные сертификаты (STCW, GMDSS, AFF, MFA, Radar и т.п.).
- Медкомиссию клади в "medical_valid_until".
- УЛМ клади в "seamans_book".
- Языки клади в "languages".
- Образование клади в "education".
- Поле "name" сертификата — только название, без номера и дат.
- Поле "number" — только номер, без названия и дат.

ПРАВИЛА ПО ОПЫТУ:
- "vessel_types" — список УНИКАЛЬНЫХ типов судов. Если моряк работал на нескольких
  судах одного типа, укажи этот тип ОДИН раз. Пример: ["Tanker", "Bulk"], а не
  ["Tanker", "Tanker", "Bulk", "Tanker"].
- "years_in_rank" — стаж в текущей должности (число лет).
- "total_sea_years" — общий морской стаж (число лет).
- "sea_service" — список записей о работе. "vessel_type" на английском (Container, Bulk, Tanker).
- "dwt" — тоннаж числом (без пробелов и запятых).

ТЕКСТ РЕЗЮМЕ:
{text}
"""


# ============================================================
# ШАГ 1: PDF → текст
# ============================================================

def extract_text_from_pdf(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")

    text_parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)

    text = "\n".join(text_parts).strip()

    if len(text) < 100:
        raise ValueError(
            "Из PDF извлечено слишком мало текста. "
            "Возможно, это скан без текстового слоя — такой файл не поддерживается."
        )

    return text


# ============================================================
# ШАГ 2: текст → JSON через Ollama
# ============================================================

def call_ollama(text: str) -> str:
    prompt = PROMPT_TEMPLATE.format(text=text)

    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",          # заставляет вернуть валидный JSON
                "options": {"temperature": TEMPERATURE},
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            "Не удалось подключиться к Ollama. "
            "Убедитесь, что Ollama запущен и слушает http://localhost:11434"
        )
    except requests.exceptions.Timeout:
        raise RuntimeError(f"Ollama не ответил за {TIMEOUT} секунд")

    return r.json()["response"]


# ============================================================
# ШАГ 3: валидация JSON
# ============================================================

def parse_and_validate(raw: str) -> Candidate:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print("❌ LLM вернула невалидный JSON")
        print("Сырой ответ:", raw[:500])
        raise

    try:
        return Candidate(**data)
    except ValidationError as e:
        print("❌ JSON не соответствует схеме:")
        print(e)
        raise


# ============================================================
# ШАГ 4: проверка на галлюцинации
# ============================================================

DATE_PATTERN = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b|\b\d{4}-\d{2}-\d{2}\b")


def find_all_dates(text: str) -> set[str]:
    """Все даты в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД, встречающиеся в тексте."""
    return set(DATE_PATTERN.findall(text))


def check_hallucinations(candidate: Candidate, source_text: str) -> list[str]:
    """Возвращает список предупреждений о возможных галлюцинациях."""
    warnings = []
    dates_in_text = find_all_dates(source_text)

    # Проверка дат сертификатов
    for cert in candidate.certificates:
        for field_name in ("issued_date", "expiry_date"):
            value = getattr(cert, field_name)
            if value and value not in dates_in_text:
                warnings.append(
                    f"Дата '{value}' ({field_name}) для сертификата "
                    f"'{cert.name}' не найдена в исходном тексте"
                )

    # Проверка медкомиссии
    if candidate.medical_valid_until and candidate.medical_valid_until not in dates_in_text:
        warnings.append(
            f"Дата медкомиссии '{candidate.medical_valid_until}' не найдена в тексте"
        )

    # Проверка УЛМ
    if candidate.seamans_book and candidate.seamans_book.valid_until:
        if candidate.seamans_book.valid_until not in dates_in_text:
            warnings.append(
                f"Дата УЛМ '{candidate.seamans_book.valid_until}' не найдена в тексте"
            )

    # Проверка названий сертификатов
    for cert in candidate.certificates:
        if cert.name and cert.name.lower() not in source_text.lower():
            warnings.append(
                f"Название сертификата '{cert.name}' не найдено в тексте — "
                f"возможна галлюцинация"
            )

    return warnings


# ============================================================
# ШАГ 5: красивый вывод
# ============================================================

def print_candidate(candidate: Candidate):
    print("\n" + "=" * 60)
    print("  РЕЗУЛЬТАТ ИЗВЛЕЧЕНИЯ")
    print("=" * 60)

    print(f"\n👤 {candidate.full_name or '—'}")
    print(f"   Дата рождения: {candidate.date_of_birth or '—'}")
    print(f"   Гражданство:   {candidate.citizenship or '—'}")
    print(f"   Email:         {candidate.email or '—'}")
    print(f"   Телефон:       {candidate.phone or '—'}")

    print(f"\n⚓ Должность:      {candidate.rank or '—'}")
    print(f"   Типы судов:     {', '.join(candidate.vessel_types) or '—'}")
    print(f"   Район плавания: {candidate.trading_area or '—'}")
    print(f"   Стаж в должн.:  {candidate.years_in_rank or '—'} лет")
    print(f"   Общий стаж:     {candidate.total_sea_years or '—'} лет")

    if candidate.sea_service:
        print(f"\n📋 Морской опыт ({len(candidate.sea_service)} записей):")
        for s in candidate.sea_service:
            dwt = f", DWT {s.dwt:,}" if s.dwt else ""
            print(f"   • {s.period or '—'}: {s.rank or '—'} на {s.vessel or '—'} "
                  f"({s.vessel_type or '—'}{dwt})")

    print(f"\n📜 Сертификаты ({len(candidate.certificates)}):")
    for c in candidate.certificates:
        num = f", № {c.number}" if c.number else ""
        issued = f", выдан {c.issued_date}" if c.issued_date else ""
        expiry = f", до {c.expiry_date}" if c.expiry_date else ""
        print(f"   • {c.name or '—'}{num}{issued}{expiry}")

    print(f"\n🏥 Медкомиссия:     {candidate.medical_valid_until or '—'}")

    if candidate.seamans_book:
        sb = candidate.seamans_book
        print(f"🪪 УЛМ:             {sb.number or '—'}, до {sb.valid_until or '—'}")

    if candidate.languages:
        langs = ", ".join(
            f"{l.language} ({l.level})" if l.level else (l.language or "—")
            for l in candidate.languages
        )
        print(f"🌐 Языки:           {langs}")

    if candidate.education:
        e = candidate.education
        parts = [p for p in [e.institution, e.specialty, str(e.year) if e.year else None] if p]
        print(f"🎓 Образование:     {', '.join(parts)}")

    print()


# ============================================================
# MAIN
# ============================================================

def process(path: Path, save_json: bool = True):
    print(f"\n📄 Файл: {path}")
    print(f"🤖 Модель: {MODEL}")

    # 1. PDF → текст
    print("\n[1/4] Извлекаю текст из PDF...")
    text = extract_text_from_pdf(path)
    print(f"      Получено {len(text)} символов")

    # 2. LLM
    print(f"\n[2/4] Отправляю в Ollama ({MODEL})...")
    raw = call_ollama(text)
    print(f"      Получено {len(raw)} символов")

    # 3. Валидация
    print("\n[3/4] Валидирую JSON...")
    candidate = parse_and_validate(raw)
    print("      ✅ Схема корректна")

    # 4. Проверка на галлюцинации
    print("\n[4/4] Проверяю на галлюцинации...")
    warnings = check_hallucinations(candidate, text)
    if warnings:
        print(f"      ⚠️  Найдено {len(warnings)} предупреждений:")
        for w in warnings:
            print(f"         • {w}")
    else:
        print("      ✅ Галлюцинаций не обнаружено")

    # Вывод
    print_candidate(candidate)

    # Сохранение в файл
    if save_json:
        out_path = path.with_suffix(".json")
        out_path.write_text(
            candidate.model_dump_json(indent=2),
            encoding="utf-8",
        )
        print(f"💾 JSON сохранён: {out_path}\n")

    return candidate


def main():
    if len(sys.argv) < 2:
        print("Использование: python extract.py <путь_к_pdf> [ещё_файлы...]")
        print("Пример:        python extract.py test_data/resume.pdf")
        sys.exit(1)

    for arg in sys.argv[1:]:
        try:
            process(Path(arg))
        except Exception as e:
            print(f"\n❌ Ошибка при обработке {arg}: {e}\n")


if __name__ == "__main__":
    main()