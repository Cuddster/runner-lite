FROM python:3.11-slim
# v2
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "RunnerBot.py"]
