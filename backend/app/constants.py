"""Категории объектов (§6).

Коды хранятся в БД как текст, русские подписи — здесь.
Новые категории можно добавлять без изменения схемы: неизвестный код
отображается как есть.
"""

CATEGORY_LABELS: dict[str, str] = {
    "museum": "Музеи",
    "monument": "Памятники",
    "memorial": "Мемориалы",
    "gallery": "Галереи",
}

# Допустимые коды категорий для валидации (§25)
ALLOWED_CATEGORIES: set[str] = set(CATEGORY_LABELS)

# Заведения OSM-слоя (идея 28, этап H): кафе, туалеты, парковки
AMENITY_LABELS: dict[str, str] = {
    "cafe": "Кафе",
    "toilet": "Туалеты",
    "parking": "Парковки",
}
AMENITY_SINGULAR: dict[str, str] = {
    "cafe": "кафе",
    "toilet": "туалет",
    "parking": "парковка",
}


def category_label(code: str) -> str:
    return CATEGORY_LABELS.get(code, code)
