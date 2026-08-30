# TagForge —— LoRA 数据集图片打标工具
FROM python:3.12-slim

# 若 Pillow 编译遇到格式问题，取消下面两行注释
# RUN apt-get update && apt-get install -y --no-install-recommends libjpeg-turbo-progs zlib1g-dev \
#     && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/', timeout=2)" || exit 1

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]