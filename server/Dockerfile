FROM python:3.14-slim-bookworm AS builtin
RUN apt-get update && apt-get install -y --no-install-recommends libarchive-tools ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY prepare_builtin.py .
COPY repxe/wimboot_assets.py ./repxe/wimboot_assets.py
COPY repxe/assets ./repxe/assets
RUN python prepare_builtin.py

FROM python:3.14-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends wimtools fonts-dejavu-core smbclient && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY repxe ./repxe
COPY --from=builtin /build/repxe/assets ./repxe/assets
USER 1000:1000
ENV REPXE_DATA=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "repxe.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips=*"]
