import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from pydantic import BaseModel
from typing import Optional, Dict, Any

# Импортируем наш новый модуль
import semantic_match 

VACANCIES_FILE = Path("vacancies.json")
TODAY = date.today()

class Vacancy(BaseModel):
    id: int
    title: str
    vessel_type: str = "Any"
    min_years_in_rank: float= 0.0
    min_total_years: float = 0.0
    planned_contract_months: int = 6
    requirements: list[str]
    required_documents: list[str] = []

def load_vacancies() -> list[Vacancy]:
    data = json.loads(VACANCIES_FILE.read_text(encoding="utf-8"))
    return [Vacancy(**v) for v in data]

def load_candidate(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))

# --- ЭТАП 1 (БЕЗ ИЗМЕНЕНИЙ) ---
def stage1_filter(candidate: dict, vacancy: Vacancy) -> tuple[bool, list[str]]:
    reasons = []

    # 1. Стаж в должности (проверяем только если вакансия этого требует)
    if vacancy.min_years_in_rank > 0:
        years_in_rank = candidate.get("years_in_rank")
        if years_in_rank is None:
            reasons.append(f"не указан стаж в должности (требуется {vacancy.min_years_in_rank} лет)")
        elif years_in_rank < vacancy.min_years_in_rank:
            reasons.append(f"стаж в должности {years_in_rank} лет < требуемых {vacancy.min_years_in_rank}")

    # 2. Общий стаж (проверяем только если вакансия этого требует)
    if vacancy.min_total_years > 0:
        total_years = candidate.get("total_sea_years")
        if total_years is None:
            reasons.append(f"не указан общий стаж (требуется {vacancy.min_total_years} лет)")
        elif total_years < vacancy.min_total_years:
            reasons.append(f"общий стаж {total_years} лет < требуемых {vacancy.min_total_years}")

    # 3. Тип судна (Умная проверка)
    vac_vessel = vacancy.vessel_type.lower().strip()
    
    # Если вакансия подходит на любой тип судна — пропускаем проверку
    if vac_vessel not in ["any", "любой", "все"]:
        cand_vessels = [v.lower() for v in candidate.get("vessel_types", [])]
        match_found = False
        
        for cv in cand_vessels:
            # Проверяем частичное совпадение (например, "Container" в "Container Ship")
            if vac_vessel in cv or cv in vac_vessel:
                match_found = True
                break
                
        if not match_found:
            reasons.append(
                f"нет опыта на типе судна '{vacancy.vessel_type}' "
                f"(есть: {', '.join(candidate.get('vessel_types', [])) or '—'})"
            )

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


def check_admin_documents(candidate: dict, vacancy: Vacancy) -> list[str]:
    missing_docs = []
    
    # 1. Извлекаем блок документов из JSON (если его нет, берем пустой словарь)
    docs_block = candidate.get("documents") or {}
    med_block = candidate.get("medical_info") or {}
    
    # 2. Собираем БАЗОВЫЕ (жестко заданные) документы
        # 2. Собираем БАЗОВЫЕ (жестко заданные) документы
    base_docs = {
        "passport_rf": docs_block.get("passport_rf"),
        "zagran_passport": docs_block.get("zagran_passport"),
        "sid": docs_block.get("sid"),                # УЛМ
        "seamans_book": docs_block.get("sid"),       # Алиас для УЛМ (если так написано в вакансии)
        "schengen_visa": docs_block.get("schengen_visa"),
        "bank_account": docs_block.get("bank_account"),
        "inn": docs_block.get("inn"),
        "snils": docs_block.get("snils"),
        "military_id": docs_block.get("military_id"),
        "discharge_book": docs_block.get("discharge_book"),  # Мореходная книжка
        "work_book": docs_block.get("work_book"),
        
        # Медицина (только из med_block)
        "medical_valid_until": med_block.get("valid_until"),
        "medical_book": med_block.get("medical_book"),
        "medical_714": med_block.get("medical_714"),
        "drug_test": med_block.get("drug_test"),
        "yellow_fever": med_block.get("yellow_fever"),
        "psych_test": med_block.get("psych_test"),
        "san_minimum": med_block.get("san_minimum"),
    }
    
    # 3. Достаем ДИНАМИЧЕСКИЕ документы (всё остальное, что LLM нашла в анкете)
    extra_docs = docs_block.get("extra_docs") or {}
    
    # 4. Объединяем всё в один большой словарь
    # Если в extra_docs есть ключ, который совпадает с базовым, он его перезапишет (это нормально)
    all_cand_docs = {**base_docs, **extra_docs}
    
    # 5. Проверяем, что требует вакансия
    for req_doc in getattr(vacancy, "required_documents", []):
        # Если ключа нет или значение пустое (None, "", []), значит документа нет
        if not all_cand_docs.get(req_doc):
            
            # Красивые названия для отчета HR
            doc_names = {
                "passport_rf": "Паспорт РФ",
                "zagran_passport": "Загранпаспорт",
                "schengen_visa": "Виза ШЕНГЕН",
                "bank_account": "Реквизиты счета",
                "inn": "ИНН",
                "snils": "СНИЛС",
                "military_id": "Военный билет",
                "discharge_book": "Мореходная книжка",
                "work_book": "Трудовая книжка",
                "seamans_book": "УЛМ (Удостоверение моряка)",
                "medical_valid_until": "Медкомиссия (срок действия)",
                "medical_book": "Медкнижка",
                "medical_714": "Заключение по 714 приказу",
                "drug_test": "Драгтест",
                "yellow_fever": "Желтая лихорадка",
                "psych_test": "Психтест (ХТИПНД)",
                "san_minimum": "Сан. минимум",
            }

            # Если ключа нет в словаре переводов, выведем его как есть (на английском)
            missing_docs.append(doc_names.get(req_doc, req_doc))

    return missing_docs


def stage2_check(candidate: dict, vacancy: Vacancy) -> dict:

    if not candidate.get("certificates"):
        # Если совсем нет сертификатов, возможно, стоит подождать
        return {
            "passed": False, 
            "status": "pending",
            "missing": ["Сертификаты не загружены"],
            "expiring": []
        }

    if not candidate.get("certificates"):
        return {
            "passed": False, 
            "status": "pending",
            "missing": ["Сертификаты не загружены"],
            "expiring": []
        }

    # 1. Проверяем базовые документы
    missing_admin_docs = check_admin_documents(candidate, vacancy)

    # 2. Проверяем сертификаты через ИИ
    candidate_cert_names = [c["name"] for c in candidate.get("certificates", []) if c.get("name")]
    cert_result = semantic_match.check_documents(vacancy.requirements, candidate_cert_names)
    
    # 3. Проверяем сроки
    expiring = []
    contract_end = TODAY + timedelta(days=vacancy.planned_contract_months * 30)
    
    for cert in candidate.get("certificates", []):
        exp_date = parse_date_robust(cert.get("expiry_date"))
        if exp_date and exp_date <= contract_end:
            expiring.append({
                "name": cert.get("name") or "Сертификат без названия", 
                "expiry": cert.get("expiry_date")
            })
            
    # Проверяем медкомиссию отдельно
    med_valid = (candidate.get("medical_info") or {}).get("valid_until")
    med_exp = parse_date_robust(med_valid)
    if med_exp and med_exp <= contract_end:
        expiring.append({"name": "Медкомиссия", "expiry": med_valid})

    # Объединяем всё в итоговый результат
    final_missing = missing_admin_docs + cert_result["missing"]
    
    return {
        "passed": len(final_missing) == 0 and len(expiring) == 0,
        "status": "checked",
        "missing": final_missing,
        "expiring": expiring,
        "matched": cert_result["matched"]
    }

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
        print("\nЭТАП 2 — ДОКУМЕНТЫ:")
        
        # Обработка статуса "Ожидание"
        if stage2.get("status") == "pending":
            print("  ⏳ ОЖИДАНИЕ (Кандидат еще не загрузил документы)")
            print("     • Опыт проверен, ожидаем сертификаты для полной проверки.")
        elif stage2["passed"]:
            print("  ✅ ПРОЙДЕН (все требования покрыты)")
        else:
            print("  ⚠️ ЕСТЬ ЗАМЕЧАНИЯ")
            if stage2["missing"]:
                print(f"     Отсутствуют: {', '.join(stage2['missing'])}")
            if stage2["expiring"]:
                print(f"     Истекают: {', '.join([str(e.get('name') or 'Неизвестно') for e in stage2['expiring']])}")

        # Вывод альтернатив (он должен быть вне блока ожидания)
        print("\nАЛЬТЕРНАТИВЫ:")
        # ...
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

    # Шаг 1: Оцениваем ВСЕ вакансии ровно один раз
    print(f"\n⏳ Оцениваем {len(vacancies)} вакансий для кандидата...")
    evaluated_results = {}
    
    for v in vacancies:
        s1_passed, s1_reasons = stage1_filter(candidate, v)
        s2_result = {"passed": False, "missing": [], "expiring": []}
        
        # Спрашиваем LLM только если кандидат прошел по опыту
        if s1_passed:
            s2_result = stage2_check(candidate, v)
            
        evaluated_results[v.id] = {
            "stage1": (s1_passed, s1_reasons),
            "stage2": s2_result
        }

    # Шаг 2: Выводим отчет и ищем альтернативы среди УЖЕ оцененных вакансий
    for v in vacancies:
        res = evaluated_results[v.id]
        s1 = res["stage1"]
        s2 = res["stage2"]
        
        alts = []
        # Если кандидат прошел эту вакансию, ищем альтернативы в других
        if s1[0]:
            for alt_v in vacancies:
                if alt_v.id == v.id: 
                    continue
                
                alt_res = evaluated_results[alt_v.id]
                # Альтернатива должна проходить и по опыту, и по документам
                if alt_res["stage1"][0] and alt_res["stage2"]["passed"]:
                    alts.append({"id": alt_v.id, "title": alt_v.title, "vessel_type": alt_v.vessel_type})
                    
        print_report(candidate, v, s1, s2, alts)

if __name__ == "__main__":
    main()