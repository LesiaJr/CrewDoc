import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from pydantic import BaseModel
from typing import Optional

# Импортируем наш новый модуль
import semantic_match 

VACANCIES_FILE = Path("vacancies.json")
TODAY = date.today()

class Vacancy(BaseModel):
    id: int
    title: str
    vessel_type: str
    min_years_in_rank: float
    min_total_years: float
    planned_contract_months: int = 6
    requirements: list[str]

def load_vacancies() -> list[Vacancy]:
    data = json.loads(VACANCIES_FILE.read_text(encoding="utf-8"))
    return [Vacancy(**v) for v in data]

def load_candidate(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))

# --- ЭТАП 1 (БЕЗ ИЗМЕНЕНИЙ) ---
def stage1_filter(candidate: dict, vacancy: Vacancy) -> tuple[bool, list[str]]:
    reasons = []
    years_in_rank = candidate.get("years_in_rank") or 0
    total_years = candidate.get("total_sea_years") or 0
    vessel_types = [v.lower() for v in candidate.get("vessel_types", [])]

    if years_in_rank < vacancy.min_years_in_rank:
        reasons.append(f"стаж в должности {years_in_rank} < {vacancy.min_years_in_rank}")
    if total_years < vacancy.min_total_years:
        reasons.append(f"общий стаж {total_years} < {vacancy.min_total_years}")
    if vacancy.vessel_type.lower() not in vessel_types:
        reasons.append(f"нет опыта на '{vacancy.vessel_type}'")

    return (len(reasons) == 0, reasons)

def parse_date_robust(value: Optional[str]) -> Optional[date]:
    """Пытается распарсить дату в разных форматах, которые может выдать LLM."""
    if not value:
        return None
    
    value = value.strip()
    
    # Если LLM написала "бессрочно", "без даты", "null" - это не дата
    if not any(char.isdigit() for char in value):
        return None
        
    # Пробуем разные форматы
    formats = ["%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d %B %Y"]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
            
    # Если это просто год "2029", ставим конец года
    if len(value) == 4 and value.isdigit():
        return date(int(value), 12, 31)
        
    return None

def stage2_check(candidate: dict, vacancy: Vacancy) -> dict:
    candidate_cert_names = [c["name"] for c in candidate.get("certificates", []) if c.get("name")]
    result = semantic_match.check_documents(vacancy.requirements, candidate_cert_names)
    
    expiring = []
    contract_end = TODAY + timedelta(days=vacancy.planned_contract_months * 30)
    
    # Проверяем даты ВСЕХ сертификатов кандидата (упрощенный, но надежный подход)
    for cert in candidate.get("certificates", []):
        exp_date = parse_date_robust(cert.get("expiry_date"))
        
        if exp_date and exp_date <= contract_end:
            expiring.append({
                "name": cert["name"], 
                "expiry": cert["expiry_date"],
                "days_left": (exp_date - TODAY).days
            })

    return {
        "passed": result["passed"] and len(expiring) == 0,
        "missing": result["missing"],
        "expiring": expiring,
        "matched": result["matched"]
    }

# --- ПОДБОР АЛЬТЕРНАТИВЫ ---
def find_alternatives(candidate: dict, vacancies: list[Vacancy], current_id: int) -> list[dict]:
    alternatives = []
    for v in vacancies:
        if v.id == current_id: continue
        
        # Сначала проверяем опыт (быстро)
        if not stage1_filter(candidate, v)[0]: 
            continue
            
        # Если опыт подходит, проверяем документы
        if stage2_check(candidate, v)["passed"]:
            alternatives.append({"id": v.id, "title": v.title, "vessel_type": v.vessel_type})
    return alternatives

# --- ОТЧЕТ ---
def print_report(candidate: dict, vacancy: Vacancy, stage1: tuple, stage2: dict, alternatives: list):
    print("\n" + "=" * 60)
    print(f"👤 {candidate.get('full_name')} | 🎯 {vacancy.title} ({vacancy.vessel_type})")

    passed1, reasons1 = stage1
    print("\nЭТАП 1 — ОПЫТ:")
    if passed1: print("  ✅ ПРОЙДЕН")
    else:
        print("  ❌ НЕ ПРОЙДЕН")
        for r in reasons1: print(f"     • {r}")

    if passed1:
        print("\nЭТАП 2 — ДОКУМЕНТЫ (Гибридный ИИ):")
        if stage2["passed"]:
            print("  ✅ ПРОЙДЕН (все требования покрыты)")
        else:
            print("  ⚠️ ЕСТЬ ЗАМЕЧАНИЯ")
            if stage2["missing"]:
                print(f"     Отсутствуют: {', '.join(stage2['missing'])}")
            if stage2["expiring"]:
                print(f"     Истекают: {', '.join([e['name'] for e in stage2['expiring']])}")

        print("\nАЛЬТЕРНАТИВЫ:")
        if alternatives:
            for a in alternatives: print(f"  ✅ {a['title']} ({a['vessel_type']})")
        else:
            print("  — не найдено")


def main():
    if len(sys.argv) < 2:
        print("python compare.py <candidate.json>")
        sys.exit(1)

    candidate_path = Path(sys.argv[1])
    candidate = load_candidate(candidate_path)
    vacancies = load_vacancies()

    for v in vacancies:
        s1_passed, s1_reasons = stage1_filter(candidate, v)

        if not s1_passed:
            # Если не прошел опыт, даже не запускаем LLM
            s2 = {"passed": False, "missing": [], "expiring": []}
            alts = []
        else:
            # Запускаем LLM только если опыт ОК
            s2 = stage2_check(candidate, v)
            alts = find_alternatives(candidate, vacancies, v.id)

        print_report(candidate, v, (s1_passed, s1_reasons), s2, alts)

if __name__ == "__main__":
    main()