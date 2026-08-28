# ─── Caddy с плагином входа ───
# Плагин не входит в стандартную сборку, поэтому Caddy собирается через xcaddy.
# Он даёт страницу входа, сессии, второй фактор и выход — всё, чего у fava нет.
FROM caddy:2-builder AS caddy-build
RUN xcaddy build --with github.com/greenpau/caddy-security

# ─── рантайм ───
FROM python:3.12-slim

# git нужен для истории правок леджера, ca-certificates — чтобы push по https
# доходил до репозитория.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=caddy-build /usr/bin/caddy /usr/local/bin/caddy

WORKDIR /app

# Зависимости отдельным слоем: меняются реже кода, и пересборка не тянет их заново.
COPY requirements.txt requirements-deploy.txt ./
RUN pip install --no-cache-dir -r requirements-deploy.txt

COPY finance/ ./finance/
COPY tools/ ./tools/
COPY deploy/ ./deploy/
COPY rules.example.yaml accounts.example.yaml fava_import_config.py fava_ext.py ./
# Ни леджера, ни настоящих rules.yaml и accounts.yaml в образе НЕТ: они живут
# в отдельном приватном репозитории и приезжают на том при старте. Так личных
# данных не оказывается ни в кодовом репозитории, ни в реестре образов —
# в образе только образцы, по которым собирается пустой скелет.

# Выписки, леджер, правила и список счетов — всё на томе.
# FINANCE_APP — где лежит код. Нужен мостику ledger/fava_ext.py: расширения
# fava ищет рядом с main.beancount, то есть на томе, а код в образе.
ENV FINANCE_INBOX=/data/inbox \
    FINANCE_LEDGER=/data/ledger \
    FINANCE_APP=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1

EXPOSE 8080

ENTRYPOINT ["python", "/app/deploy/entrypoint.py"]
