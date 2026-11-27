FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /app
CMD ["python", "bot.py"]
