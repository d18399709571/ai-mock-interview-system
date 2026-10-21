FROM python:3.12-slim
WORKDIR /runtime
COPY ai-interview-backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
ENV PYTHONDONTWRITEBYTECODE=1
