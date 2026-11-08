"""
extract.py — извлечение структурированных данных из резюме моряка.
"""

import json
import re
import sys
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass

import pdfplumber
import requests
from pydantic import BaseModel, Field, ValidationError, field_validator

# ============================================================
# КОНФИГ
# ============================================================

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:7b-instruct-q4_K_M"          
TEMPERATURE = 0
TIMEOUT = 900                   

# ============================================================
# СХЕМА (Pydantic)
# ============================================================

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


def _to_int(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    s = re.sub(r"[^\d]", "", str(v))
    return int(s) if s else None


def _to_float(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def _to_str(v):
    if v is None:
        return None
    return str(v)


class SeaService(BaseModel):
    period: Optional[str] = None
    rank: Optional[str] = None
    vessel: Optional[str] = None
    vessel_type: Optional[str] = None
    dwt: Optional[int] = None

    @field_validator("dwt", mode="before")
    @classmethod
    def coerce_dwt(cls, v):
        return _to_int(v)


class Documents(BaseModel):
    """
    Фиксированные поля + extra_docs для всего незнакомого.
    Любые НЕИЗВЕСТНЫЕ ключи, пришедшие от LLM на верхнем уровне,
    автоматически перекладываются в extra_docs — схема остаётся стабильной,
    а downstream-код по-прежнему видит docs.extra_docs.
    """
    passport_rf: Optional[str] = None
    zagran_passport: Optional[str] = None
    sid: Optional[str] = None
    schengen_visa: Optional[str] = None
    bank_account: Optional[str] = None
    inn: Optional[str] = None
    snils: Optional[str] = None
    military_id: Optional[str] = None
    discharge_book: Optional[str] = None
    work_book: Optional[str] = None
    extra_docs: Dict[str, Optional[str]] = Field(default_factory=dict)


    @model_validator(mode="before")
    @classmethod
    def absorb_unknown_into_extra(cls, data):
        if not isinstance(data, dict):
            return data
        known = {
            "passport_rf", "zagran_passport", "sid", "schengen_visa",
            "bank_account", "inn", "snils", "military_id",
            "discharge_book", "extra_docs",
        }
        extra = data.get("extra_docs")
        if not isinstance(extra, dict):
            extra = {}
        for k in [k for k in data.keys() if k not in known]:
            extra[k] = data.pop(k)
        data["extra_docs"] = extra
        return data

class MedicalInfo(BaseModel):
    medical_book: Optional[str] = None      # Медкнижка
    medical_714: Optional[str] = None       # Заключение по 714 приказу
    drug_test: Optional[str] = None         # Драгтест
    yellow_fever: Optional[str] = None      # Желтая лихорадка
    psych_test: Optional[str] = None        # Психтест (ХТИПНД)
    san_minimum: Optional[str] = None       # Сан. минимум
    valid_until: Optional[str] = None 

class Certificate(BaseModel):
    name: Optional[str] = None
    number: Optional[str] = None
    issued_date: Optional[str] = None
    expiry_date: Optional[str] = None

    @field_validator("number", mode="before")
    @classmethod
    def number_to_str(cls, v):
        return _to_str(v)


class Language(BaseModel):
    language: Optional[str] = None
    level: Optional[str] = None


class Education(BaseModel):
    institution: Optional[str] = None
    specialty: Optional[str] = None
    year: Optional[int] = None

    @field_validator("year", mode="before")
    @classmethod
    def coerce_year(cls, v):
        return _to_int(v)


class Candidate(BaseModel):
    full_name: Optional[str] = None
    date_of_birth: Optional[str] = None
    citizenship: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    rank: Optional[str] = None
    documents: Optional[Documents] = None
    vessel_types: list[str] = Field(default_factory=list)
    medical_info: Optional[MedicalInfo] = None
    trading_area: Optional[str] = None
    years_in_rank: Optional[float] = None
    total_sea_years: Optional[float] = None
    sea_service: list[SeaService] = Field(default_factory=list)
    certificates: list[Certificate] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    education: Optional[Education] = None
    extra_info: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("years_in_rank", "total_sea_years", mode="before")
    @classmethod
    def coerce_years(cls, v):
        return _to_float(v)

    @field_validator("sea_service", mode="after")
    @classmethod
    def dedupe_sea_service(cls, v: list[SeaService]) -> list[SeaService]:
        seen = set()
        result = []
        for item in v:
            # ключ для дедупликации: период + название судна
            key = (item.period or "").strip().lower(), (item.vessel or "").strip().lower()
            if key[0] and key not in seen:
                seen.add(key)
                result.append(item)
        return result
    
    @model_validator(mode="before")
    @classmethod
    def fix_documents_list(cls, data):
        """Safety net: если LLM вернула documents списком — конвертируем в объект."""
        if isinstance(data, dict) and isinstance(data.get("documents"), list):
            new_docs: dict = {"extra_docs": {}}
            for doc in data["documents"]:
                if not isinstance(doc, dict):
                    continue
                doc_type = str(doc.get("type") or doc.get("name") or "").lower()
                doc_num = doc.get("number") or doc.get("document_number") or doc.get("value")
                t = doc_type

                # УЛМ / SID
                if (
                    "улм" in t
                    or "удостоверение личности моряка" in t
                    or "seafarer" in t and ("identity" in t or "id" in t)
                    or "sid" == t.strip()
                    or "sid" in t
                ):
                    new_docs["sid"] = doc_num
                elif "zagran" in t or "загран" in t:
                    new_docs["zagran_passport"] = doc_num
                elif "passport" in t or "паспорт" in t:
                    new_docs["passport_rf"] = doc_num
                elif "discharge" in t or "мореход" in t or ("seaman" in t and "book" in t):
                    new_docs["discharge_book"] = doc_num
                else:
                    new_docs["extra_docs"][doc_type] = doc_num
            data["documents"] = new_docs

        return data

    @field_validator("vessel_types", mode="after")
    @classmethod
    def dedupe_vessel_types(cls, v: list[str]) -> list[str]:
        seen, out = set(), []
        for item in v:
            if not isinstance(item, str):
                continue
            key = item.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(item.strip())
        return out


# ============================================================
# ПРОМПТ
# ============================================================

PROMPT_FULL = """Ты — экстрактор данных из резюме моряка. Верни ТОЛЬКО валидный JSON
строго по схеме ниже. Никаких комментариев, пояснений, markdown, обёрток ```json.

СХЕМА ОТВЕТА:
{
  "_reasoning": null,
  "full_name": null, "date_of_birth": null, "citizenship": null,
  "email": null, "phone": null, "rank": null, "trading_area": null,
  "total_sea_years": null, "years_in_rank": null,
  "documents": {
    "passport_rf": null, "zagran_passport": null, "sid": null,
    "schengen_visa": null, "bank_account": null, "inn": null,
    "snils": null, "military_id": null, "discharge_book": null,
    "work_book": null, "extra_docs": {}
  },
  "medical_info": {
    "medical_book": null, "medical_714": null, "drug_test": null,
    "yellow_fever": null, "psych_test": null, "san_minimum": null,
    "valid_until": null
  },
  "sea_service": [{"period": null, "rank": null, "vessel": null, "vessel_type": null, "dwt": null}],
  "vessel_types": [],
  "certificates": [{"name": null, "number": null, "issued_date": null, "expiry_date": null}],
  "languages": [{"language": null, "level": null}],
  "education": {"institution": null, "specialty": null, "year": null},
  "extra_info": {}
}

ГЛАВНЫЕ ПРАВИЛА:
- Поля без данных → null. Массивы без данных → [].
- НИКОГДА не выдумывай значения и не подставляй номера "по шаблону".
- НИКОГДА не возвращай шаблоны форматов ("DD.MM.YYYY", "string", "example").
  Если данных нет — null.
- Не добавляй поля, которых нет в схеме. Не копируй исходный текст в ответ.
- В поле "_reasoning" кратко (одной строкой) опиши спорные классификации
  документов: "<метка из текста> - это <ключ>, потому что <причина>".
  Если спорных нет — null.

ОБЩИЕ ПОЛЯ:
- full_name — ФИО полностью.
- date_of_birth — дата рождения (как в тексте).
- citizenship — гражданство (как в тексте).
- email — строка с "@". phone — с кодом страны.
- rank — текущая должность (Position/Rank/Должность из шапки).
- trading_area — район плавания (если указан явно).
- total_sea_years — "Общий стаж: N лет".
- years_in_rank — "Стаж в должности: N лет".

ДОКУМЕНТЫ (documents) — определяй по СУТИ, не по названию:
- sid — Удостоверение личности моряка (УЛМ, SID, Seafarer's Identity Document).
  СУТЬ: документ ЛИЧНОСТИ. Карточка с фото, подтверждает КТО вы.
  Аналог паспорта для моряков. Номер — код страны + цифры (RUS1234567).
- discharge_book — Мореходная книжка (Seaman's Book, CDC, Discharge Book).
  СУТЬ: документ СЛУЖБЫ. Журнал рейсов: суда, должности, периоды.
  Подтверждает ОПЫТ. Это НЕ документ личности.
- passport_rf — Паспорт РФ (внутренний, 4+6 цифр).
- zagran_passport — Загранпаспорт.
- schengen_visa — Виза Шенген.
- bank_account — Расчётный/банковский счёт.
- inn — ИНН.
- snils — СНИЛС.
- military_id — Военный билет, приписное.
- work_book — Трудовая книжка.
- Всё остальное — в extra_docs: {"название": "номер"}.

⚠️ ЧАСТО ПУТАЮТ:
- УЛМ / SID = документ ЛИЧНОСТИ → sid.
- Мореходная книжка = документ СЛУЖБЫ → discharge_book.
  Даже если рядом с номером УЛМ написано слово "моряка" — это sid,
  а не discharge_book.
- Сертификаты и дипломы (Certificate of Competency, GMDSS, Basic Safety) —
  это НЕ documents. Их номера сюда не клади, они идут в certificates.

МЕДИЦИНА (medical_info):
- medical_book — Медкнижка / медицинская книжка.
- medical_714 — Заключение по 714 приказу.
- drug_test — Драгтест / тест на наркотики.
- yellow_fever — Желтая лихорадка / Yellow Fever.
- psych_test — Психтест / ХТИПНД.
- san_minimum — Сан. минимум.
- valid_until — общая дата "годен до", окончание медкомиссии.
Если упоминаний меддокументов в тексте нет — оставь все поля null.
Не переноси сюда сертификаты.

SEA_SERVICE — морской опыт:
- Одна запись = один период.
- period — реальные даты "DD.MM.YYYY - DD.MM.YYYY" из текста.
  Строки "DD.MM.YYYY" — это шаблон, их НЕ копируй как значение.
  Если в тексте нет реальных дат — null.
- rank — текст ДО дефиса: "Boatswain - Container Ship" → "Boatswain".
- vessel_type — текст ПОСЛЕ дефиса: → "Container Ship".
- vessel — после "Vessel Name", "M/V", "MV".
- dwt — целое после "DWT". Берётся ТОЛЬКО из блока этого же судна.
  Нет DWT в блоке — null. Не копируй из соседних записей.

VESSEL_TYPES — уникальные vessel_type из sea_service.
  ⚠️ НЕ клади сюда названия судов и не клади rank.

CERTIFICATES — сертификаты и дипломы:
- name — полное название (как в тексте).
- number — только номер.
- issued_date, expiry_date — как в тексте.
⚠️ Не дублируй здесь паспорт, SID, медкнижку — для них есть documents
   и medical_info.

LANGUAGES — языки: [{"language": "English", "level": "Fluent"}].

EDUCATION — учебное заведение, специальность, год окончания.

ТЕКСТ АНКЕТЫ:
<<<TEXT>>>
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

    if len(text) < 50:
        raise ValueError("Слишком мало текста. Возможно, это скан без текстового слоя.")

    return text


# ============================================================
# ШАГ 2: текст → JSON через Ollama
# ============================================================

def call_ollama(prompt_template: str, text: str) -> str:
    prompt = prompt_template.replace("<<<TEXT>>>", text)

    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": TEMPERATURE,
                    "num_ctx": 16384,        # ← без этого длинный промпт+текст обрежется
                    "num_predict": 8192,
                    # "num_parallel":1,
                    "num_batch": 1024,
                    },
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Не удалось подключиться к Ollama.")
    except requests.exceptions.Timeout:
        raise RuntimeError(f"Ollama не ответил за {TIMEOUT} секунд")

    return r.json()["response"]

def _clean_response(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    if data.get("documents") is None:
        data["documents"] = {"extra_docs": {}}
    elif isinstance(data["documents"], dict):
        data["documents"].setdefault("extra_docs", {})
    for key in ("vessel_types", "sea_service", "certificates", "languages"):
        if data.get(key) is None:
            data[key] = []
    if data.get("education") is None:
        data["education"] = {}
    return data

def extract_candidate(text: str) -> Candidate:
    # 👇 ОДИН вызов вместо двух
    raw = call_ollama(PROMPT_FULL, text) 
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Модель вернула невалидный JSON: {e}\n{raw[:500]}")

    data = _clean_response(data)
    data.pop("_reasoning", None)   # Убираем reasoning, чтобы не засорять JSON

    return Candidate(**data)



def _period_days(period: str | None) -> int:
    if not period:
        return 0
    m = re.search(r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})", period)
    if not m:
        return 0
    try:
        d1 = datetime.strptime(m.group(1), "%d.%m.%Y")
        d2 = datetime.strptime(m.group(2), "%d.%m.%Y")
        return max((d2 - d1).days, 0)
    except ValueError:
        return 0

def enrich_experience(c: Candidate, text: str = "") -> Candidate:
    # 1) regex-fallback из шапки — «Общий стаж: 4 года», «Стаж в должности: 2 года»
    if c.total_sea_years is None and text:
        m = re.search(r"[Оо]бщий\s+стаж\s*[:\-]?\s*(\d+(?:[.,]\d+)?)", text)
        if m:
            c.total_sea_years = float(m.group(1).replace(",", "."))

    if c.years_in_rank is None and text:
        m = re.search(r"[Сс]таж\s+в\s+должности\s*[:\-]?\s*(\d+(?:[.,]\d+)?)", text)
        if m:
            c.years_in_rank = float(m.group(1).replace(",", "."))

    # 2) расчёт из sea_service — только как fallback
    total_days = sum(_period_days(s.period) for s in c.sea_service)
    calc_total = round(total_days / 365.25, 1) if total_days else None

    target_rank = (c.rank or "").strip().lower()
    rank_days = sum(
        _period_days(s.period)
        for s in c.sea_service
        if (s.rank or "").strip().lower() == target_rank
    )
    calc_rank = round(rank_days / 365.25, 1) if rank_days else None

    if c.total_sea_years is None:
        c.total_sea_years = calc_total
    if c.years_in_rank is None:
        c.years_in_rank = calc_rank
    return c

# ============================================================
# ШАГ 3: Валидация и проверка на галлюцинации
# ============================================================

# def parse_and_validate(raw: str) -> Candidate:
#     try:
#         data = json.loads(raw)
#     except json.JSONDecodeError as e:
#         print("❌ LLM вернула невалидный JSON:")
#         print(f"Сырой ответ (первые 500 символов): {raw[:500]}")
#         raise e

#     try:
#         return Candidate(**data)
#     except ValidationError as e:
#         print("❌ JSON не соответствует схеме:")
#         print(e)
#         print(f"\nСырой ответ (первые 500 символов): {raw[:500]}")
#         raise e

DATE_PATTERN = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b|\b\d{4}\b")

@dataclass
class HallucinationWarning:
    field: str       # машинный id поля, напр. "documents.sid", "sea_service[0].period"
    message: str     # человеческое описание

    def __str__(self) -> str:
        return f"⚠️ ПРОВЕРИТЬ ВРУЧНУЮ [{self.field}]: {self.message}"

def check_hallucinations(candidate: Candidate, source_text: str) -> list[HallucinationWarning]:
    """
    Проверяет поля Candidate на галлюцинации.
    НЕ мутирует candidate. Возвращает список предупреждений с id поля.
    """
    warnings: list[HallucinationWarning] = []
    dates_in_text = set(DATE_PATTERN.findall(source_text))
    clean_text = re.sub(r"\s+", "", source_text.lower())
    source_lower = source_text.lower()

    PLACEHOLDERS = {"есть", "есть (без номера)", "выдан", "нет", "n/a", "б/н"}

    def _flag(field: str, msg: str) -> None:
        warnings.append(HallucinationWarning(field=field, message=msg))

    # ============================================================
    # 1. СЕРТИФИКАТЫ
    # ============================================================
    for i, cert in enumerate(candidate.certificates):
        if cert.name and cert.name.lower() not in source_lower:
            _flag(f"certificates[{i}].name", f"'{cert.name}' не найдено в тексте")
        for field_name in ("issued_date", "expiry_date"):
            value = getattr(cert, field_name)
            if value and value not in dates_in_text:
                _flag(
                    f"certificates[{i}].{field_name}",
                    f"дата '{value}' для '{cert.name or '—'}' не найдена в тексте",
                )

    # ============================================================
    # 2. МЕДИЦИНА
    # ============================================================
    if candidate.medical_info:
        med = candidate.medical_info
        for fname in MedicalInfo.model_fields:
            fval = getattr(med, fname)
            if not fval:
                continue
            if fname == "valid_until":
                if fval not in dates_in_text:
                    _flag(f"medical_info.{fname}",
                          f"дата медкомиссии '{fval}' не найдена в тексте")
            else:
                if re.sub(r"\s+", "", str(fval).lower()) not in clean_text:
                    _flag(f"medical_info.{fname}",
                          f"'{fval}' не найдено в тексте")

    # ============================================================
    # 3. ДОКУМЕНТЫ
    # ============================================================
    if candidate.documents:
        docs = candidate.documents
        for fname in Documents.model_fields:
            if fname == "extra_docs":
                continue
            fval = getattr(docs, fname)
            if not fval or not isinstance(fval, str):
                continue
            if fval.strip().lower() in PLACEHOLDERS:
                continue
            if re.sub(r"\s+", "", fval.lower()) not in clean_text:
                _flag(f"documents.{fname}", f"'{fval}' не найдено в тексте")

        for key, fval in docs.extra_docs.items():
            if not fval or not isinstance(fval, str):
                continue
            if fval.strip().lower() in PLACEHOLDERS:
                continue
            if re.sub(r"\s+", "", fval.lower()) not in clean_text:
                _flag(f"documents.extra_docs.{key}",
                      f"'{fval}' не найдено в тексте")

    # ============================================================
    # 4. SEA_SERVICE
    # ============================================================
    for i, s in enumerate(candidate.sea_service):
        label = s.rank or s.vessel or f"#{i + 1}"

        if s.period:
            period_dates = re.findall(r"\d{2}\.\d{2}\.\d{4}", s.period)
            if not period_dates:
                _flag(f"sea_service[{i}].period",
                      f"'{s.period}' ({label}) не содержит реальных дат")
            else:
                missing = [d for d in period_dates if d not in dates_in_text]
                if missing:
                    _flag(f"sea_service[{i}].period",
                          f"'{s.period}' ({label}) содержит даты, которых нет: {missing}")

        if s.vessel and re.sub(r"\s+", "", s.vessel.lower()) not in clean_text:
            _flag(f"sea_service[{i}].vessel",
                  f"'{s.vessel}' ({label}) не найдено в тексте")

        if s.dwt:
            dwt_variants = {
                str(s.dwt),
                f"{s.dwt:,}",
                f"{s.dwt:,}".replace(",", " "),
                f"{s.dwt:,}".replace(",", "."),
            }
            if not any(v in source_text for v in dwt_variants):
                _flag(f"sea_service[{i}].dwt",
                      f"DWT {s.dwt} ({label}) не найден в тексте")

    # ============================================================
    # 5. ЧИСЛОВЫЕ ПОЛЯ КАНДИДАТА
    # ============================================================
    for fname in ("total_sea_years", "years_in_rank"):
        fval = getattr(candidate, fname)
        if fval is None:
            continue
        num_str = str(fval)
        if num_str.endswith(".0"):
            num_str = num_str[:-2]
        num_pattern = r"(?<!\d)" + re.escape(num_str) + r"(?!\d)"
        if not re.search(num_pattern, source_text):
            _flag(fname, f"{fval} — число не найдено в тексте")

    # ============================================================
    # 6. ОБРАЗОВАНИЕ
    # ============================================================
    if candidate.education:
        edu = candidate.education
        if edu.institution and edu.institution.lower() not in source_lower:
            _flag("education.institution",
                  f"'{edu.institution}' не найдено в тексте")
        if edu.specialty and edu.specialty.lower() not in source_lower:
            _flag("education.specialty",
                  f"'{edu.specialty}' не найдена в тексте")

    return warnings



# ============================================================
# ШАГ 4: Красивый вывод
# ============================================================
def _warned_fields(warnings: list[HallucinationWarning]) -> set[str]:
    return {w.field for w in warnings}


def _is_warned(prefix: str, marks: set[str]) -> bool:
    """
    True, если есть предупреждение точно по `prefix` или по вложенному полю.
    Например, prefix="sea_service[0]" сработает на "sea_service[0].period".
    """
    return any(
        m == prefix
        or m.startswith(prefix + ".")
        or m.startswith(prefix + "[")
        for m in marks
    )


def _flag(prefix: str, marks: set[str]) -> str:
    return " ⚠️" if _is_warned(prefix, marks) else ""

def print_candidate(
    candidate: Candidate,
    warnings: list[HallucinationWarning] | None = None,
):
    marks = _warned_fields(warnings or [])

    print("\n" + "=" * 60)
    print("  РЕЗУЛЬТАТ ИЗВЛЕЧЕНИЯ")
    print("=" * 60)

    print(f"\n👤 {candidate.full_name or '—'}")
    print(f"   Email: {candidate.email or '—'} | Тел: {candidate.phone or '—'}")

    print(f"\n⚓ Должность:      {candidate.rank or '—'}")
    print(f"   Типы судов:     {', '.join(candidate.vessel_types) or '—'}")
    print(f"   Стаж в должн.:  {candidate.years_in_rank or '—'} лет"
          f"{_flag('years_in_rank', marks)}")
    print(f"   Общий стаж:     {candidate.total_sea_years or '—'} лет"
          f"{_flag('total_sea_years', marks)}")

    if candidate.sea_service:
        print(f"\n📋 Морской опыт ({len(candidate.sea_service)} записей):")
        for i, s in enumerate(candidate.sea_service):
            dwt = f", DWT {s.dwt:,}" if s.dwt else ""
            print(
                f"   • {s.period or '—'}: {s.rank or '—'} "
                f"на {s.vessel or '—'} ({s.vessel_type or '—'}{dwt})"
                f"{_flag(f'sea_service[{i}]', marks)}"
            )

    print(f"\n📜 Сертификаты ({len(candidate.certificates)}):")
    for i, c in enumerate(candidate.certificates):
        num = f", № {c.number}" if c.number else ""
        expiry = f", до {c.expiry_date}" if c.expiry_date else ""
        print(f"   • {c.name or '—'}{num}{expiry}"
              f"{_flag(f'certificates[{i}]', marks)}")

    # 🏥 МЕДИЦИНА
    if candidate.medical_info:
        med = candidate.medical_info
        if any([med.valid_until, med.medical_book, med.medical_714, med.drug_test,
                med.yellow_fever, med.psych_test, med.san_minimum]):
            print("\n🏥 Медицинские документы:")
            if med.valid_until:   print(f"   • Медкомиссия до:   {med.valid_until}{_flag('medical_info.valid_until', marks)}")
            if med.medical_book:  print(f"   • Медкнижка:        {med.medical_book}{_flag('medical_info.medical_book', marks)}")
            if med.medical_714:   print(f"   • Заключение 714:   {med.medical_714}{_flag('medical_info.medical_714', marks)}")
            if med.drug_test:     print(f"   • Драгтест:         {med.drug_test}{_flag('medical_info.drug_test', marks)}")
            if med.yellow_fever:  print(f"   • Желтая лихорадка: {med.yellow_fever}{_flag('medical_info.yellow_fever', marks)}")
            if med.psych_test:    print(f"   • Психтест:         {med.psych_test}{_flag('medical_info.psych_test', marks)}")
            if med.san_minimum:   print(f"   • Сан. минимум:     {med.san_minimum}{_flag('medical_info.san_minimum', marks)}")

    # 📄 БАЗОВЫЕ ДОКУМЕНТЫ
    if candidate.documents:
        docs = candidate.documents

        MEDICAL_TOKENS = ("мед", "medical", "health", "drug", "псих",
                          "yellow", "san", "714", "госпитал", "hospital")
        filtered_extra = {
            k: v for k, v in docs.extra_docs.items()
            if v and not any(tok in k.lower() for tok in MEDICAL_TOKENS)
        }

        has_base = any([
            docs.passport_rf, docs.zagran_passport, docs.sid, docs.schengen_visa,
            docs.bank_account, docs.inn, docs.snils, docs.military_id,
            docs.discharge_book, docs.work_book,
        ])
        has_extra = bool(filtered_extra)

        if has_base or has_extra:
            print("\n📄 Базовые документы:")
            if docs.passport_rf:     print(f"   • Паспорт РФ:        {docs.passport_rf}{_flag('documents.passport_rf', marks)}")
            if docs.zagran_passport: print(f"   • Загранпаспорт:     {docs.zagran_passport}{_flag('documents.zagran_passport', marks)}")
            if docs.sid:             print(f"   • УЛМ (SID):         {docs.sid}{_flag('documents.sid', marks)}")
            if docs.schengen_visa:   print(f"   • Виза Шенген:       {docs.schengen_visa}{_flag('documents.schengen_visa', marks)}")
            if docs.bank_account:    print(f"   • Реквизиты счета:   {docs.bank_account}{_flag('documents.bank_account', marks)}")
            if docs.inn:             print(f"   • ИНН:               {docs.inn}{_flag('documents.inn', marks)}")
            if docs.snils:           print(f"   • СНИЛС:             {docs.snils}{_flag('documents.snils', marks)}")
            if docs.military_id:     print(f"   • Военный билет:     {docs.military_id}{_flag('documents.military_id', marks)}")
            if docs.discharge_book:  print(f"   • Мореходная книжка: {docs.discharge_book}{_flag('documents.discharge_book', marks)}")
            if docs.work_book:       print(f"   • Трудовая книжка:   {docs.work_book}{_flag('documents.work_book', marks)}")

            for k, v in filtered_extra.items():
                print(f"   • {k}: {v}{_flag(f'documents.extra_docs.{k}', marks)}")

    if candidate.education and any([
        candidate.education.institution, candidate.education.specialty, candidate.education.year,
    ]):
        edu = candidate.education
        print("\n🎓 Образование:")
        if edu.institution: print(f"   • Учебное заведение: {edu.institution}{_flag('education.institution', marks)}")
        if edu.specialty:   print(f"   • Специальность:     {edu.specialty}{_flag('education.specialty', marks)}")
        if edu.year:        print(f"   • Год:               {edu.year}")

    if candidate.extra_info:
        print("\n📦 Прочая информация (extra_info):")
        for k, v in candidate.extra_info.items():
            print(f"   • {k}: {v}")

    # сводка
    if warnings:
        print(f"\n⚠️  Требует ручной проверки: {len(warnings)} поле(й)")
        for w in warnings:
            print(f"   • [{w.field}] {w.message}")

    print()
    


# ============================================================
# MAIN
# ============================================================

def process(path: Path):
    
    print(f"\n📄 Файл: {path}")
    print(f"🤖 Модель: {MODEL}")

    print("\n[1/4] Извлекаю текст из PDF...")
    text = extract_text_from_pdf(path)
    print(f"      Получено {len(text)} символов")

    print(f"\n[2/4] Отправляю в Ollama ({MODEL}) [1 вызов]...")
    candidate = extract_candidate(text)
    candidate = enrich_experience(candidate, text)
    print("      ✅ Схема корректна")
    print("\n[4/4] Проверяю на галлюцинации...")
    
    has_real_experience = (
        bool(candidate.rank) or 
        bool(candidate.total_sea_years) or 
        bool(candidate.years_in_rank) or 
        any(s.rank or s.vessel for s in candidate.sea_service)
    )
    if not has_real_experience:
        candidate.vessel_types = []
        candidate.sea_service = []

    warnings = check_hallucinations(candidate, text)
    if warnings:
        print(f"      ⚠️  Найдено {len(warnings)} предупреждений (требуют ручной проверки):")
        for w in warnings:
            print(f"         • [{w.field}] {w.message}")
    else:
        print("      ✅ Галлюцинаций не обнаружено")

    print_candidate(candidate, warnings)   # 👈 передаём warnings

    out_path = path.with_suffix(".json")
    out_path.write_text(candidate.model_dump_json(indent=2), encoding="utf-8")
    print(f"💾 JSON сохранён: {out_path}\n")

    return candidate

def main():
    if len(sys.argv) < 2:
        print("Использование: python extract.py <путь_к_pdf> [ещё_файлы...]")
        sys.exit(1)

    for arg in sys.argv[1:]:
        try:
            process(Path(arg))
        except Exception as e:
            print(f"\n❌ Ошибка при обработке {arg}: {e}\n")

if __name__ == "__main__":
    main()