FROM python:3.13-alpine

WORKDIR /app
RUN apk add --no-cache ffmpeg

COPY app.py /app/app.py

ENV PORT=5004 \
    CONFIG_DIR=/config \
    PUBLIC_BASE_URL= \
    TRANSCODE_UPSCALING=false \
    TRANSCODE_RESOLUTION=1920x1080 \
    TRANSCODE_SHARPEN=0.5 \
    TRANSCODE_DENOISE=1.0

EXPOSE 5004
VOLUME ["/config"]

USER nobody
CMD ["python", "/app/app.py"]
