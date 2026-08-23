# ─── Caddy с плагином второго фактора ───
# Плагин не входит в стандартную сборку, поэтому Caddy собирается через xcaddy.
FROM caddy:2-builder AS caddy-build
RUN xcaddy build --with github.com/steffenbusch/caddy-postauth-2fa

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
COPY rules.example.yaml accounts.example.yaml fava_import_config.py ./
# Ни леджера, ни настоящих rules.yaml и accounts.yaml в образе НЕТ: они живут
# в отдельном приватном репозитории и приезжают на том при старте. Так личных
# данных не оказывается ни в кодовом репозитории, ни в реестре образов —
# в образе только образцы, по которым собирается пустой скелет.

# Выписки, леджер, правила и список счетов — всё на томе.
ENV FINANCE_INBOX=/data/inbox \
    FINANCE_LEDGER=/data/ledger \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1

EXPOSE 8080

ENTRYPOINT ["python", "/app/deploy/entrypoint.py"]
