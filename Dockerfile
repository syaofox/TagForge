# TagForge —— LoRA 数据集图片打标工具
FROM python:3.12-slim

# 若 Pillow 编译遇到格式问题，取消下面两行注释
# RUN apt-get update && apt-get install -y --no-install-recommends libjpeg-turbo-progs zlib1g-dev \
#     && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py core.py llm_client.py templates static ./

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]