"""Deterministic symptom routing; no LLM call needed to choose specialists."""

import re
from .profiles import resolve


def words(text):
    stop = {
        "и",
        "в",
        "на",
        "с",
        "со",
        "из",
        "к",
        "по",
        "но",
        "а",
        "или",
        "что",
        "как",
        "не",
        "нет",
        "без",
        "для",
        "это",
        "его",
        "за",
        "при",
        "от",
        "the",
        "a",
        "an",
        "is",
        "are",
        "and",
        "or",
        "of",
        "to",
        "in",
        "with",
        "without",
        "not",
    }
    return set(re.findall(r"[\w-]+", text.lower())) - stop


def rank_components(available, question, target_id=None, observations=None, direct=False):
    observations = observations or {}
    target = next((c for c in available if c["id"] == target_id), None)
    direct_ids = set(target.get("dependencies", [])) if target else set()
    sources = {s["component_id"] for s in target.get("context_sources", [])} if target else set()
    query = words(question)
    ranked = []
    for order, c in enumerate(available):
        p = resolve(c)
        overlap = query & words(c["name"] + " " + c.get("description", "") + " " + " ".join(p["keywords"]))
        failed = observations.get(c["id"], {}).get("ok") is False
        if c["id"] == target_id:
            group, reason = 0, "Целевой компонент"
        elif c["id"] in direct_ids:
            group, reason = 1, "Прямая зависимость цели"
        elif c["id"] in sources:
            group, reason = 2, "Источник контекста цели"
        elif target_id and target_id in c.get("dependencies", []):
            group, reason = 3, "Обратная зависимость цели"
        else:
            group, reason = 4, "Совпадение с симптомом" if overlap else "Проверка окружения"
        score = len(overlap) * 3 + (2 if failed else 0)
        reason += (": " + ", ".join(sorted(overlap))) if overlap else ""
        if failed:
            reason += "; ошибка текущей проверки"
        ranked.append(((group if target else 0, -score, order), c, reason))
    ranked.sort(key=lambda item: item[0])
    limit = 1 if direct else 6
    return [(c, reason) for _, c, reason in ranked[:limit]], [
        {"component_id": c["id"], "component": c["name"], "reason": "Лимит специалистов"} for _, c, _ in ranked[limit:]
    ]


def context_requests(chosen, available, question, window, trace_id):
    by_id = {c["id"]: c for c in available}
    requests, skipped, source_ids = {}, [], set()
    for target in chosen:
        for link in target.get("context_sources", []):
            source = by_id.get(link["component_id"])
            if not source or not source["enabled"]:
                skipped.append(
                    {
                        "component_id": link["component_id"],
                        "target_id": target["id"],
                        "reason": "Источник недоступен или выключен",
                    }
                )
                continue
            if source["id"] not in source_ids and len(source_ids) >= 8:
                skipped.append(
                    {"component_id": source["id"], "target_id": target["id"], "reason": "Лимит восьми источников"}
                )
                continue
            source_ids.add(source["id"])
            action = "metrics" if source["type"] == "prometheus" else "search"
            args = {
                "action": action,
                "service_name": link.get("service_name", ""),
                "query": link.get("query", ""),
                "check_name": link.get("check_name", ""),
                "time_window_minutes": window,
                "trace_id": trace_id,
            }
            if source["type"] in {"openproject", "wikijs", "mattermost"} and not args["query"]:
                # A bounded symptom term is useful to knowledge APIs; full prose is not.
                terms = sorted(words(question) & set(resolve(target)["keywords"]))
                args["query"] = terms[0] if terms else target["name"][:120]
            key = (source["id"], *args.values())
            if key not in requests:
                requests[key] = {"source": source, "args": args, "targets": []}
            requests[key]["targets"].append(target["id"])
    return list(requests.values()), skipped
