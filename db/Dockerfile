# PostgreSQL 18 + PostGIS 3.6 + pgvector
# Базовый образ PostGIS (Debian 13 trixie) уже содержит расширение postgis.
# pgvector ставится готовым пакетом из официального репозитория PGDG
# (нужен для семантического поиска, §27) — компиляция из исходников не требуется.
FROM postgis/postgis:18-3.6

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-18-pgvector \
    && rm -rf /var/lib/apt/lists/*

# Каталог инициализации: расширения создаются до первого подключения
COPY init/*.sql /docker-entrypoint-initdb.d/
