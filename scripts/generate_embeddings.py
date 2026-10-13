"""Этап 7.1: эмбеддинги объектов для семантического поиска (§27).

Текст объекта (name + description + themes + keywords) -> вектор через
локальный inference server (Ollama, EMBEDDING_MODEL=nomic-embed-text).
Векторы хранятся в PostgreSQL pgvector.

Запуск: python scripts/generate_embeddings.py  (после import_data.py)
Идемпотентно: объекты с уже заполненным embedding пропускаются
(флаг --force пересчитывает все).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url.replace("postgresql+psycopg://", "postgresql://")
    user = os.environ.get("POSTGRES_USER", "culturalfind")
    pwd = os.environ.get("POSTGRES_PASSWORD", "culturalfind")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "culturalfind")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"


def embedding_text(obj: dict) -> str:
    """Текст для эмбеддинга (§27: name + description + themes + keywords)."""
    parts = [obj["name"]]
    if obj.get("short_description"):
        parts.append(obj["short_description"])
    if obj.get("description"):
        parts.append(obj["description"][:600])
    if obj.get("themes"):
        parts.append("темы: " + ", ".join(obj["themes"]))
    if obj.get("keywords"):
        parts.append("ключевые слова: " + ", ".join(obj["keywords"]))
    if obj.get("category"):
        parts.append(obj["category"])
    return "\n".join(p for p in parts if p)


def embed(base_url: str, model: str, text: str) -> list[float]:
    payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))["embedding"]


def main() -> None:
    try:
        import psycopg
    except ImportError:
        raise SystemExit("Нужен psycopg: pip install -r backend/requirements.txt")

    force = "--force" in sys.argv
    base_url = os.environ.get("AI_BASE_URL", "http://localhost:11434")
    model = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")
    url = db_url()

    print(f"Эмбеддинг-модель: {model} @ {base_url}")
    # проверка доступности
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=5) as r:
            names = [m["name"] for m in json.loads(r.read())["models"]]
        if not any(model in n for n in names):
            print(f"ВНИМАНИЕ: модель {model} не найдена. Выполните: ollama pull {model}")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"AI inference server недоступен ({exc}). Запустите ollama serve")

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            if force:
                cur.execute("SELECT id, name, category, description, short_description, themes, keywords")
            else:
                cur.execute(
                    """SELECT id, name, category, description, short_description, themes, keywords
                       FROM cultural_objects WHERE embedding IS NULL"""
                )
            rows = cur.fetchall()
            print(f"Объектов к обработке: {len(rows)}")

            updated = 0
            start = time.time()
            for obj_id, name, category, description, short_desc, themes, keywords in rows:
                text = embedding_text({
                    "name": name, "category": category, "description": description,
                    "short_description": short_desc, "themes": themes, "keywords": keywords,
                })
                vector = embed(base_url, model, text)
                cur.execute(
                    "UPDATE cultural_objects SET embedding = %s WHERE id = %s",
                    (json.dumps(vector), obj_id),
                )
                updated += 1
                if updated % 25 == 0:
                    conn.commit()  # частичный коммит: сбой не потеряет готовое
                    print(f"  {updated}/{len(rows)} ({time.time() - start:.1f}s)")
            conn.commit()

    print(f"Готово: обновлено {updated} векторов за {time.time() - start:.1f}s")


if __name__ == "__main__":
    main()
