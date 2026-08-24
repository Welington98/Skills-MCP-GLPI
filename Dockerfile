# Skills-MCP-GLPI — imagem do servidor MCP GLPI (Streamable HTTP).
#
# Uso:
#   docker build -t skills-mcp-glpi .
#   docker run -p 8824:8824 --env-file .env skills-mcp-glpi
#   docker compose up -d            (usa docker-compose.yml)
#
# Configuracao vem do ambiente (--env-file .env ou GLPI_MCP_CONFIG):
#   GLPI_BASE_URL / GLPI_APP_TOKEN / GLPI_USER_TOKEN (vazio de proposito)
#   LOG_FILE=/tmp/mcp-glpi.log                       (ver compose)

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Usuario nao-root: o servidor roda como mcp, nunca como root.
RUN groupadd --system mcp \
    && useradd --system --gid mcp --home-dir /app mcp

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/logs && chown -R mcp:mcp /app

USER mcp

EXPOSE 8824

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        urllib.request.urlopen('http://127.0.0.1:8824/health', timeout=3); \
        sys.exit(0)" || exit 1

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8824"]
