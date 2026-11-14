FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 1000 --create-home ember
ENV TIKTOKEN_CACHE_DIR=/opt/ember-tokenizer
RUN python -c "import tiktoken; tiktoken.get_encoding('o200k_base')" && chmod -R a+rX /opt/ember-tokenizer
COPY ember ./ember
COPY config ./config
COPY sidecar.py ./
RUN mkdir -p /app/data && chown -R 1000:1000 /app
USER 1000:1000
ENV EMBER_HOST=0.0.0.0 EMBER_SIDECAR_PORT=8767 EMBER_DATA_ROOT=/app/data EMBER_REQUIRE_TOKEN=1 EMBER_ENABLE_PROPOSALS=0 PYTHONUNBUFFERED=1
EXPOSE 8767
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "import json,os,urllib.request as u; r=u.urlopen(u.Request('http://127.0.0.1:8767/health',headers={'Authorization':'Bearer '+os.environ['EMBER_TOKEN']}),timeout=3); b=json.load(r); r.close(); assert b.get('ok') is True and b.get('service')=='ember'"]
CMD ["python", "sidecar.py"]
