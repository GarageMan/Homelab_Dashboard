FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD [ "/run.sh" ]
