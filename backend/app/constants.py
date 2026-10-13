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


def category_label(code: str) -> str:
    return CATEGORY_LABELS.get(code, code)
