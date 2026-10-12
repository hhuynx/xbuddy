FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 XBUDDY_HOST=0.0.0.0 XBUDDY_PORT=8080
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY server ./server
RUN mkdir -p /app/data
VOLUME ["/app/data"]
EXPOSE 8080
CMD ["python", "-m", "server.main"]
