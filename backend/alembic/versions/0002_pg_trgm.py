"""pg_trgm и триграммные индексы по именам: опечатки в названиях мест (этап D).

Резолвер (§26) при полном промахе LIKE-проходов делает запасной проход по
схожести (`similarity(...)`) — «двоцовая площадь» находит «Дворцовую площадь».
Индексы построены по тому же выражению, что и LIKE-запросы резолвера
(`lower(replace(name, 'ё', 'е'))`), поэтому ускоряют и точные проходы.

Revision ID: 0002_pg_trgm
Revises: 0001_initial
Create Date: 2026-10-16
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002_pg_trgm"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NAME_EXPR = "lower(replace(name, 'ё', 'е'))"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_locations_name_trgm "
        f"ON locations USING gin ({_NAME_EXPR} gin_trgm_ops)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_cultural_objects_name_trgm "
        f"ON cultural_objects USING gin ({_NAME_EXPR} gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_cultural_objects_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_locations_name_trgm")
    # расширение не удаляем: pg_trgm не мешает, а откат чужого кода его сломает
