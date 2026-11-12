# RTMP Multiview (developed with AI) - web multiviewer in a container.
#   docker build -t rtmp-multiview .
#   docker run -d -p 8080:8080 -e MV_PASSWORD=... -v ./config:/config rtmp-multiview
FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-numpy python3-pil ffmpeg fonts-dejavu-core ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# unprivileged user; /config holds config.json (stream keys) and must be writable by uid 10001
RUN useradd --system --uid 10001 --home-dir /config --shell /usr/sbin/nologin multiview \
 && mkdir -p /config /app && chown multiview:multiview /config

COPY multiview.py /app/multiview.py

ENV MV_CONFIG=/config/config.json \
    MV_REQUIRE_PASSWORD=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER multiview
WORKDIR /config
VOLUME /config
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

ENTRYPOINT ["python3", "/app/multiview.py"]
CMD ["--headless"]
