import json
import re
from pathlib import Path
import os

PROFILES_DIR = Path("profiles")
PROFILES_DIR.mkdir(exist_ok=True)

def get_profile_id(candidate_data: dict) -> str:
    """Создает уникальный ID профиля на основе Email или ФИО."""
    email = candidate_data.get("email")
    if email:
        # Убираем недопустимые символы из email для имени файла
        safe_email = re.sub(r'[^a-zA-Z0-9@.]', '', email)
        return safe_email
    
    name = candidate_data.get("full_name", "unknown").replace(" ", "_")
    return name

def merge_profiles(old: dict, new: dict) -> dict:
    """Умное слияние старого и нового профиля."""
    merged = old.copy()
    
    for key, value in new.items():
        if value is None or value == [] or value == "":
            continue

        # Особый случай: сертификаты
        if key == "certificates" and isinstance(value, list):
            old_certs = merged.get("certificates", [])
            cert_dict = {}
            for c in old_certs + value:
                cert_num = c.get("number") or c.get("name")
                if cert_num:
                    cert_dict[cert_num] = c
            merged["certificates"] = list(cert_dict.values())
            
        # Особый случай: словари (документы, медицина, доп. инфо)
        elif key in ["documents", "medical_info", "extra_info"] and isinstance(value, dict):
            old_dict = merged.get(key) or {}
            
            # Если это документы, аккуратно сливаем extra_docs
            if key == "documents":
                old_extra = old_dict.get("extra_docs") or {}
                new_extra = value.get("extra_docs") or {}
                old_extra.update(new_extra)
                value["extra_docs"] = old_extra
            
            # Обновляем старый словарь новыми значениями
            old_dict.update(value)
            merged[key] = old_dict
            
        else:
            merged[key] = value
            
    return merged


def update_profile(new_data_path: Path):
    new_data = json.loads(new_data_path.read_text(encoding="utf-8"))
    
    profile_id = get_profile_id(new_data)
    profile_file = PROFILES_DIR / f"{profile_id}.json"
    
    if profile_file.exists():
        print(f"🔄 Найден существующий профиль: {profile_id}. Дополняем...")
        old_data = json.loads(profile_file.read_text(encoding="utf-8"))
        final_data = merge_profiles(old_data, new_data)
    else:
        print(f"✨ Создан новый профиль: {profile_id}")
        final_data = new_data
    
    # 👇 ИСПРАВЛЕННЫЙ СПИСОК (без опечаток и с правильными путями)
    core_docs = ["passport_rf", "zagran_passport", "sid", "schengen_visa",
                 "bank_account", "inn", "snils", "military_id", "discharge_book"]
    
    docs_block = final_data.get("documents") or {}
    med_block = final_data.get("medical_info") or {}
    
    missing_core_docs = []
    for doc_key in core_docs:
        if not docs_block.get(doc_key):
            missing_core_docs.append(doc_key)
   
    # Проверяем медкомиссию (лежит в medical_info)
    if not med_block.get("medical_book"):
        missing_core_docs.append("medical_book")
    if not med_block.get("valid_until"):
        missing_core_docs.append("medical_valid_until")

    # Умное определение статуса
    has_experience = bool(final_data.get("total_sea_years") and final_data.get("rank"))
    has_certs = bool(final_data.get("certificates"))
    
    if has_experience and has_certs and not missing_core_docs:
        final_data["profile_status"] = "READY"
    elif has_experience and (not has_certs or missing_core_docs):
        final_data["profile_status"] = "DOCS_PENDING"
    elif has_certs and not has_experience:
        final_data["profile_status"] = "EXPERIENCE_PENDING"
    else:
        final_data["profile_status"] = "INCOMPLETE"

    if missing_core_docs:
        print(f"⚠️ Профиль не готов. Не хватает: {', '.join(missing_core_docs)}")

    # Сохраняем
    profile_file.write_text(json.dumps(final_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 Профиль сохранен: {profile_file}")
    print(f"📌 Статус: {final_data['profile_status']}")
    
    # Удаляем исходник
    if new_data_path.resolve() != profile_file.resolve():
        try:
            new_data_path.unlink()
            print(f"🗑️ Исходный файл {new_data_path.name} удален.")
        except Exception as e:
            print(f"⚠️ Не удалось удалить исходный файл: {e}")

    return profile_file

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Использование: python profile_manager.py <путь_к_файлу.json>")
        sys.exit(1)

    file_path = Path(sys.argv[1])
    if not file_path.exists():
        print(f"❌ Файл не найден: {file_path}")
        sys.exit(1)

    update_profile(file_path)
