"""
compare.py — этап 2: сравнение кандидата с вакансиями.

Пайплайн:
1. Загрузить JSON кандидата (от extract.py) и vacancies.json
2. Этап 1: фильтр по опыту (детерминированно)
3. Этап 2: проверка сертификатов (наличие + сроки)
4. Подбор альтернативной вакансии
5. Отчёт
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from pydantic import BaseModel


# ============================================================
# КОНФИГ
# ============================================================

VACANCIES_FILE = Path("vacancies.json")
SYNONYMS_FILE = Path("cert_synonyms.json")
TODAY = date.today()
EXPIRY_WARNING_DAYS = 90      # сертификат считается "истекающим", если < 90 дней


# ============================================================
# СХЕМА ВАКАНСИИ
# ============================================================

class Vacancy(BaseModel):
    id: int
    title: str
    vessel_type: str
    min_years_in_rank: float
    min_total_years: float
    trading_area: Optional[str] = None
    planned_contract_months: int = 6
    required_certs: list[str]


# ============================================================
# ЗАГРУЗКА
# ============================================================

def load_vacancies() -> list[Vacancy]:
    data = json.loads(VACANCIES_FILE.read_text(encoding="utf-8"))
    return [Vacancy(**v) for v in data]


def load_synonyms() -> dict[str, list[str]]:
    if not SYNONYMS_FILE.exists():
        return {}
    return json.loads(SYNONYMS_FILE.read_text(encoding="utf-8"))


def load_candidate(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ============================================================
# УТИЛИТЫ ДЛЯ ДАТ
# ============================================================

def parse_date(value: Optional[str]) -> Optional[date]:
    """Парсит дату в форматах ДД.ММ.ГГГГ или ГГГГ-ММ-ДД."""
    if not value:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def days_until(d: Optional[date]) -> Optional[int]:
    if not d:
        return None
    return (d - TODAY).days


# ============================================================
# СИНОНИМЫ СЕРТИФИКАТОВ
# ============================================================

def normalize_cert_name(name: str, synonyms: dict[str, list[str]]) -> str:
    """Приводит название сертификата к каноническому виду."""
    if not name:
        return ""
    name_lower = name.strip().lower()
    for canonical, aliases in synonyms.items():
        if canonical.lower() == name_lower:
            return canonical
        for alias in aliases:
            if alias.lower() == name_lower:
                return canonical
    return name.strip()


def build_cert_index(certificates: list[dict], synonyms: dict) -> dict[str, dict]:
    """
    Возвращает словарь: каноническое_название → сертификат.
    Если сертификатов с одинаковым каноном несколько — берём тот,
    у которого срок действия позже.
    """
    index: dict[str, dict] = {}
    for cert in certificates:
        canon = normalize_cert_name(cert.get("name", ""), synonyms)
        if not canon:
            continue
        existing = index.get(canon)
        if existing is None:
            index[canon] = cert
        else:
            # Берём тот, у которого срок позже
            old_exp = parse_date(existing.get("expiry_date"))
            new_exp = parse_date(cert.get("expiry_date"))
            if new_exp and (not old_exp or new_exp > old_exp):
                index[canon] = cert
    return index


# ============================================================
# ЭТАП 1: ФИЛЬТР ПО ОПЫТУ
# ============================================================

def stage1_filter(candidate: dict, vacancy: Vacancy) -> tuple[bool, list[str]]:
    """
    Возвращает (passed, reasons).
    reasons — список причин отказа, если passed=False.
    """
    reasons = []

    # Стаж в должности
    years_in_rank = candidate.get("years_in_rank")
    if years_in_rank is None:
        reasons.append(
            f"не указан стаж в должности (требуется {vacancy.min_years_in_rank} лет)"
        )
    elif years_in_rank < vacancy.min_years_in_rank:
        reasons.append(
            f"стаж в должности {years_in_rank} лет "
            f"< требуемых {vacancy.min_years_in_rank}"
        )

    # Общий стаж
    total_years = candidate.get("total_sea_years")
    if total_years is None:
        reasons.append(
            f"не указан общий стаж (требуется {vacancy.min_total_years} лет)"
        )
    elif total_years < vacancy.min_total_years:
        reasons.append(
            f"общий стаж {total_years} лет "
            f"< требуемых {vacancy.min_total_years}"
        )

    # Тип судна
    vessel_types = [v.lower() for v in candidate.get("vessel_types", [])]
    if vacancy.vessel_type.lower() not in vessel_types:
        reasons.append(
            f"нет опыта на типе судна '{vacancy.vessel_type}' "
            f"(есть: {', '.join(candidate.get('vessel_types', [])) or '—'})"
        )

    return (len(reasons) == 0, reasons)


# ============================================================
# ЭТАП 2: ПРОВЕРКА ДОКУМЕНТОВ
# ============================================================

def stage2_check(candidate: dict, vacancy: Vacancy, synonyms: dict) -> dict:
    """
    Возвращает:
    {
      "passed": bool,
      "missing": [...],
      "expiring": [{name, expiry, days_left}],
      "unknown": [...]
    }
    """
    cert_index = build_cert_index(candidate.get("certificates", []), synonyms)
    contract_end = TODAY + timedelta(days=vacancy.planned_contract_months * 30)

    missing = []
    expiring = []

    for req in vacancy.required_certs:
        cert = cert_index.get(req)
        if cert is None:
            missing.append(req)
            continue

        expiry = parse_date(cert.get("expiry_date"))
        if expiry is None:
            continue  # срок не указан — считаем валидным, но это странно

        # Сертификат истекает до конца контракта?
        if expiry <= contract_end:
            expiring.append({
                "name": req,
                "expiry": cert.get("expiry_date"),
                "days_left": (expiry - TODAY).days,
            })
        # Или истекает в ближайшие 90 дней?
        elif (expiry - TODAY).days <= EXPIRY_WARNING_DAYS:
            expiring.append({
                "name": req,
                "expiry": cert.get("expiry_date"),
                "days_left": (expiry - TODAY).days,
            })

    return {
        "passed": len(missing) == 0 and len(expiring) == 0,
        "missing": missing,
        "expiring": expiring,
    }


# ============================================================
# ПОДБОР АЛЬТЕРНАТИВЫ
# ============================================================

def find_alternatives(candidate: dict, vacancies: list[Vacancy],
                      synonyms: dict, current_id: int) -> list[dict]:
    """
    Возвращает список вакансий, для которых кандидат проходит и этап 1, и этап 2.
    Исключает текущую вакансию.
    """
    alternatives = []
    for v in vacancies:
        if v.id == current_id:
            continue
        ok1, _ = stage1_filter(candidate, v)
        if not ok1:
            continue
        check2 = stage2_check(candidate, v, synonyms)
        if check2["passed"]:
            alternatives.append({
                "id": v.id,
                "title": v.title,
                "vessel_type": v.vessel_type,
            })
    return alternatives


# ============================================================
# ОТЧЁТ
# ============================================================

def print_report(candidate: dict, vacancy: Vacancy,
                 stage1: tuple[bool, list[str]],
                 stage2: dict,
                 alternatives: list[dict]):
    print("\n" + "=" * 60)
    print("  ОТЧЁТ ПО КАНДИДАТУ")
    print("=" * 60)

    print(f"\n👤 {candidate.get('full_name', '—')}")
    print(f"🎯 Вакансия: {vacancy.title} ({vacancy.vessel_type})")
    print(f"📅 План контракта: {vacancy.planned_contract_months} мес.")

    # Этап 1
    passed1, reasons1 = stage1
    print(f"\n{'─' * 60}")
    print("ЭТАП 1 — ОПЫТ")
    print(f"{'─' * 60}")
    if passed1:
        print("✅ ПРОЙДЕН")
        print(f"   Стаж в должности: {candidate.get('years_in_rank')} лет")
        print(f"   Общий стаж:       {candidate.get('total_sea_years')} лет")
        print(f"   Типы судов:       {', '.join(candidate.get('vessel_types', []))}")
    else:
        print("❌ НЕ ПРОЙДЕН")
        for r in reasons1:
            print(f"   • {r}")

    # Этап 2
    print(f"\n{'─' * 60}")
    print("ЭТАП 2 — ДОКУМЕНТЫ")
    print(f"{'─' * 60}")
    if not passed1:
        print("⏭  Пропущен (кандидат не прошёл этап 1)")
    elif stage2["passed"]:
        print("✅ ПРОЙДЕН")
        print(f"   Все обязательные сертификаты в наличии, сроки в порядке")
    else:
        print("⚠️  ЕСТЬ ЗАМЕЧАНИЯ")
        if stage2["missing"]:
            print(f"\n   Отсутствуют ({len(stage2['missing'])}):")
            for m in stage2["missing"]:
                print(f"   • {m}")
        if stage2["expiring"]:
            print(f"\n   Истекают ({len(stage2['expiring'])}):")
            for e in stage2["expiring"]:
                print(f"   • {e['name']} — до {e['expiry']} "
                      f"(осталось {e['days_left']} дней)")

    # Альтернативы
    print(f"\n{'─' * 60}")
    print("АЛЬТЕРНАТИВНЫЕ ВАКАНСИИ")
    print(f"{'─' * 60}")
    if not passed1:
        print("⏭  Не подбираются (кандидат не прошёл этап 1)")
    elif alternatives:
        for a in alternatives:
            print(f"   ✅ {a['title']} ({a['vessel_type']}) — id={a['id']}")
    else:
        print("   — Не найдено")

    print()


# ============================================================
# MAIN
# ============================================================

def process(candidate_path: Path):
    print(f"\n📄 Кандидат: {candidate_path}")

    candidate = load_candidate(candidate_path)
    vacancies = load_vacancies()
    synonyms = load_synonyms()

    print(f"📋 Загружено вакансий: {len(vacancies)}")
    print(f"📚 Синонимов сертификатов: {len(synonyms)}")

    # Проверяем по каждой вакансии
    for v in vacancies:
        stage1 = stage1_filter(candidate, v)
        stage2 = stage2_check(candidate, v, synonyms)
        alternatives = (
            find_alternatives(candidate, vacancies, synonyms, v.id)
            if stage1[0] else []
        )
        print_report(candidate, v, stage1, stage2, alternatives)


def main():
    if len(sys.argv) < 2:
        print("Использование: python compare.py <candidate.json> [ещё...]")
        print("Пример:        python compare.py test_data/resume_ideal.json")
        sys.exit(1)

    for arg in sys.argv[1:]:
        path = Path(arg)
        try:
            process(path)
        except Exception as e:
            print(f"\n❌ Ошибка при обработке {path}: {e}\n")


if __name__ == "__main__":
    main()