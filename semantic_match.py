import json
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:7b"
TIMEOUT = 300
LEVEL_WEIGHT = {"basic": 1, "advanced": 2, "unknown": 1.5}

TOPICS_CACHE = {}

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

def get_topics(items: list[str], context: str) -> list[dict]:
    if not items: return []
    
    cache_key = tuple(sorted(items))
    if cache_key in TOPICS_CACHE:
        return TOPICS_CACHE[cache_key]
        
    prompt = f"""Ты эксперт в морской отрасли (стандарты STCW). Определи тему (topic) и уровень (level) для каждого сертификата.
Верни строго JSON объект с ключом "topics", внутри которого будет массив объектов.

Формат ответа:
{{
  "topics": [
    {{"name": "оригинальное название", "topic": "тема", "level": "уровень"}}
  ]
}}

Доступные темы: basic_safety, fire_fighting, medical, navigation, radio_comm, tanker_ops, security, other.

ЖЕСТКИЕ ПРАВИЛА ОПРЕДЕЛЕНИЯ ТЕМЫ (Если видишь эти слова — ставь соответствующую тему):
- "GMDSS", "Global Maritime Distress", "GOC", "ROC" -> topic: "radio_comm"
- "AFF", "Fire Fighting", "Пожарная", "Противопожарная" -> topic: "fire_fighting"
- "MFA", "First Aid", "Медицинская помощь" -> topic: "medical"
- "Radar", "ARPA", "ECDIS", "Навигация" -> topic: "navigation"
- "Basic Safety", "Безопасность" -> topic: "basic_safety"
- "Tanker", "Танкер" -> topic: "tanker_ops"

УНИВЕРСАЛЬНЫЕ ПРАВИЛА ОПРЕДЕЛЕНИЯ УРОВНЯ (STCW):
- Уровень "basic": если в названии есть слова Basic, Awareness, First Aid, Familiarization.
- Уровень "advanced": если в названии есть слова Advanced, Management, Care, Proficiency.
- Уровень "unknown": если не можешь определить (например, просто GMDSS GOC).

Список сертификатов ({context}):
{json.dumps(items, ensure_ascii=False)}
"""
    data = _call_llm(prompt)
    
    result = []
    if isinstance(data, list): result = data
    elif isinstance(data, dict) and "topics" in data: result = data["topics"]
    
    if result:
        TOPICS_CACHE[cache_key] = result
    return result

def check_documents(vacancy_reqs: list[str], candidate_certs: list[str]) -> dict:
    vac_topics = get_topics(vacancy_reqs, "vacancy")
    cand_topics = get_topics(candidate_certs, "candidate")

    if not vac_topics:
        return {"passed": False, "missing": ["ОШИБКА LLM: Не удалось определить темы"], "matched": []}

    cand_best_levels = {}
    for c in cand_topics:
        t, l = c.get("topic"), c.get("level")
        if LEVEL_WEIGHT.get(l, 0) > LEVEL_WEIGHT.get(cand_best_levels.get(t, 0), 0):
            cand_best_levels[t] = l

    missing = []
    matched = []

    for req in vac_topics:
        req_topic = req.get("topic")
        req_level = req.get("level")
        cand_level = cand_best_levels.get(req_topic)

        # 🔥 ЛОГИКА ЛОКАЛЬНОГО МАТЧИНГА:
        if cand_level is not None:
            # Если тема совпала, и хотя бы у одного из них уровень unknown, 
            # мы доверяем локальному совпадению (чтобы не дергать судью лишний раз)
            if req_level == "unknown" or cand_level == "unknown":
                matched.append(req["name"])
                continue
                
            # Если уровни известны, сравниваем их веса
            if LEVEL_WEIGHT.get(cand_level, 0) >= LEVEL_WEIGHT.get(req_level, 0):
                matched.append(req["name"])
                continue
        
        # Если темы нет или уровень не дотягивает — идем к судье
        prompt = f"""Ты строгий эксперт по найму моряков. Оцени, закрывает ли сертификат кандидата требование вакансии.

ТРЕБОВАНИЕ ВАКАНСИИ: "{req['name']}" (Уровень: {req_level})
СЕРТИФИКАТЫ КАНДИДАТА: {json.dumps(candidate_certs, ensure_ascii=False)}

ПРАВИЛА ОЦЕНКИ:
1. Сертификат может быть записан аббревиатурой или полным названием.
   Например: GMDSS GOC = Global Maritime Distress and Safety System General Operator Certificate. 
   AFF = Advanced Fire Fighting. MFA = Medical First Aid.
2. Если названия означают одно и то же — это ЗАКРЫВАЕТ требование.
3. Базовый уровень (Basic, First Aid) НЕ ЗАКРЫВАЕТ продвинутый (Advanced, Care, Management).

Верни строго JSON: {{"covered": true/false, "reason": "почему ты так решил"}}
"""
        verdict = _call_llm(prompt)
        
        if verdict.get("covered") is True:
            matched.append(req["name"])
        else:
            missing.append(req["name"])

    return {
        "passed": len(missing) == 0,
        "missing": missing,
        "matched": matched
    }