import json
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:7b"
TIMEOUT = 300

LEVEL_WEIGHT = {"basic": 1, "advanced": 2, "unknown": 1.5}


def _call_llm(prompt: str) -> dict:
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0}
        }, timeout=TIMEOUT)
        if r.status_code != 200:
            return {}
        response_data = r.json()
        if "response" not in response_data:
            return {}
        return json.loads(response_data["response"])
    except Exception as e:
        print(f"❌ Ошибка LLM: {e}")
        return {}

def check_documents(vacancy_reqs: list[str], candidate_certs: list[str]) -> dict:
    if not vacancy_reqs or not candidate_certs:
        return {"passed": False, "missing": vacancy_reqs, "matched": []}

    # Просим LLM ТОЛЬКО определить темы (без маппинга)
    prompt = f"""Ты строгий эксперт по найму моряков (стандарты STCW). Твоя задача — определить темы (topics) и уровни (levels) для сертификатов.

ТРЕБОВАНИЯ ВАКАНСИИ:
{json.dumps(vacancy_reqs, ensure_ascii=False)}

СЕРТИФИКАТЫ КАНДИДАТА:
{json.dumps(candidate_certs, ensure_ascii=False)}

ПРАВИЛА ОЦЕНКИ:
1. Придумай тему (topic) для каждого сертификата, исходя из его смысла (например, "fire_fighting", "navigation").
2. ОБЯЗАТЕЛЬНО используй одни и те же названия тем для одинаковых по смыслу сертификатов.
3. Определи уровень: basic, advanced или unknown.

Верни строго JSON в формате:
{{
  "vacancy_topics": [
    {{"name": "название требования", "topic": "тема", "level": "уровень"}}
  ],
  "candidate_topics": [
    {{"name": "название сертификата", "topic": "тема", "level": "уровень"}}
  ]
}}
"""
    data = _call_llm(prompt)
    
    if not data or "vacancy_topics" not in data or "candidate_topics" not in data:
        return {"passed": False, "missing": ["ОШИБКА LLM: не удалось извлечь темы"], "matched": []}

    # Вывод тем в консоль
    print("\n   🔍 Как ИИ видит темы:")
    print("   [Вакансия]:")
    for item in data.get("vacancy_topics", []):
        print(f"      • {item['name']} -> {item['topic']} ({item['level']})")
    print("   [Анкета]:")
    for item in data.get("candidate_topics", []):
        print(f"      • {item['name']} -> {item['topic']} ({item['level']})")

    # --- ЛОКАЛЬНЫЙ МАТЧИНГ СИЛАМИ PYTHON ---
    # Группируем сертификаты кандидата по темам
    cand_by_topic = {}
    for c in data.get("candidate_topics", []):
        t = c.get("topic")
        if t:
            cand_by_topic.setdefault(t, []).append(c)

    matched = []
    missing_reqs = []

    # Сравниваем темы и уровни
    for req in data.get("vacancy_topics", []):
        req_name = req["name"]
        req_topic = req.get("topic")
        req_level = req.get("level", "unknown")
        
        best_match = None
        for c in cand_by_topic.get(req_topic, []):
            c_level = c.get("level", "unknown")
            # Если темы совпали, проверяем уровни
            if req_level == "unknown" or c_level == "unknown" or LEVEL_WEIGHT.get(c_level, 0) >= LEVEL_WEIGHT.get(req_level, 0):
                best_match = c["name"]
                break
                
        if best_match:
            matched.append(req_name)
        else:
            missing_reqs.append(req_name)

    # --- СУДЬЯ (FALLBACK) ---
    # Если код не нашел совпадений (нет темы или не хватает уровня), зовем LLM-судью
    final_missing = []
    for req_name in missing_reqs:
        prompt = f"""Ты строгий эксперт по найму моряков. Оцени, закрывает ли сертификат кандидата требование вакансии.

ТРЕБОВАНИЕ ВАКАНСИИ: "{req_name}"
СЕРТИФИКАТЫ КАНДИДАТА: {json.dumps(candidate_certs, ensure_ascii=False)}

ПРАВИЛА: 
1. Названия могут быть аббревиатурами или на разных языках.
2. Если сертификат по смыслу подходит — ставь covered: true. 
3. Базовый уровень (First Aid) не закрывает продвинутый (Medical Care).

Верни строго JSON: {{"covered": true/false, "reason": "почему"}}
"""
        verdict = _call_llm(prompt)
        
        if verdict.get("covered") is True:
            matched.append(req_name)
        else:
            final_missing.append(req_name)

    return {
        "passed": len(final_missing) == 0,
        "missing": final_missing,
        "matched": matched
    }