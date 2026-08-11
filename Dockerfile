FROM python:3.12-slim

ENV PORT=5616
WORKDIR /app

# 复制应用（卷挂载会覆盖此目录，见 docker-compose.yml，便于数据热更新）
COPY app/ /app/app/

EXPOSE 5616

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5616/index.html')" || exit 1

CMD ["python", "app/server.py"]
