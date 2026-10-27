FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY screener screener
COPY web web
COPY scripts scripts
ENV SCREENER_DATA=/data PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "-m", "screener", "app", "--host", "0.0.0.0", "--no-browser"]
