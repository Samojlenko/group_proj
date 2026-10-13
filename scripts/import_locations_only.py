"""Разовая загрузка таблицы locations из locations.json + landmarks.json (без трогания cultural_objects)."""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROCESSED = ROOT / "data" / "processed"

import importlib.util

spec = importlib.util.spec_from_file_location("import_data", ROOT / "scripts" / "import_data.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

try:
    import psycopg
except ImportError:
    raise SystemExit("Нужен psycopg")

url = mod.db_url()
print("Подключение:", url.split("@")[-1])
with psycopg.connect(url) as conn:
    with conn.cursor() as cur:
        n = mod.import_locations(cur)
    conn.commit()

with psycopg.connect(url) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM locations")
        print("locations в БД:", cur.fetchone()[0])
