FROM python:3.13-alpine

WORKDIR /app
COPY app.py /app/app.py

ENV PORT=5004 \
    CONFIG_DIR=/config \
    PUBLIC_BASE_URL=

EXPOSE 5004
VOLUME ["/config"]

USER nobody
CMD ["python", "/app/app.py"]
