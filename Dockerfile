FROM python:3.12-slim-bookworm

ARG APP_UID=501
ARG APP_GID=20
ARG APP_VERSION=0.6.0-dev.7.2
ARG VCS_REF=unknown
ARG BUILD_STATE=unknown

LABEL org.opencontainers.image.title="Start-stop Analysis" \
      org.opencontainers.image.description="Standalone electrochemical start-stop analysis service" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      io.start-stop-analysis.git-state="${BUILD_STATE}"

ENV DEBIAN_FRONTEND=noninteractive \
    ECHEM_CONTAINER_MODE=1 \
    HOME=/Users/hive \
    MPLCONFIGDIR=/app/scratch/matplotlib \
    START_STOP_MPL_CACHE_SEED=/app/matplotlib-cache-seed \
    START_STOP_XDG_CACHE_SEED=/app/xdg-cache-seed \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    START_STOP_FONT_BOLD=/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc \
    START_STOP_FONT_REGULAR=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc \
    START_STOP_WORKBOOK_BUILDER=/app/start_stop_analysis/material_config_workbook.py \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        fonts-noto-cjk \
        openssh-client \
        tini \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.docker.txt /tmp/requirements.docker.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/requirements.docker.txt

RUN useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --home-dir /Users/hive \
        --create-home \
        --shell /usr/sbin/nologin \
        echem \
    && mkdir -p \
        /app/docker \
        /app/echem_platform \
        /app/matplotlib-cache-seed \
        /app/scratch \
        /app/start_stop_analysis \
        /app/state/database \
        /app/state/published-cache/current \
        /app/static \
        /app/xdg-cache-seed \
        /run/start-stop-secrets \
        /Users/hive/.cache \
        /Users/hive/.ssh \
    && chmod 700 /run/start-stop-secrets /Users/hive/.ssh \
    && chown -R "${APP_UID}:${APP_GID}" /app /run/start-stop-secrets /Users/hive

COPY --chown=${APP_UID}:${APP_GID} start_stop_service.py ./
COPY --chown=${APP_UID}:${APP_GID} echem_platform/start_stop*.py ./echem_platform/
COPY --chown=${APP_UID}:${APP_GID} \
    static/start-stop.css \
    static/start-stop-config.css \
    static/start-stop-config.html \
    static/start-stop-config.js \
    static/start-stop-cv-eis.css \
    static/start-stop-cv-eis.html \
    static/start-stop-cv-eis.js \
    static/start-stop-materials.html \
    static/start-stop-materials.js \
    static/start-stop-shell.js \
    static/start-stop-workstations.css \
    static/start-stop-workstations.html \
    static/start-stop-workstations.js \
    static/start-stop.html \
    static/start-stop.js \
    static/styles.css \
    static/workbench.css \
    ./static/
COPY --chown=${APP_UID}:${APP_GID} static/icons/gear.svg ./static/icons/
COPY --chown=${APP_UID}:${APP_GID} docker/collection-config.json ./docker/
COPY --chown=${APP_UID}:${APP_GID} docker/start_stop_analysis/*.py ./start_stop_analysis/
COPY --chown=${APP_UID}:${APP_GID} \
    scripts/create_start_stop_backup.py \
    scripts/run_start_stop_backup_scheduler.py \
    scripts/write_start_stop_backup_status.py \
    ./scripts/

USER ${APP_UID}:${APP_GID}

# Build the font discovery cache once into the immutable image. Runtime jobs
# copy this small seed into their writable tmpfs instead of rescanning the CJK
# font collection on the first PDF export.
RUN MPLCONFIGDIR=/app/matplotlib-cache-seed \
    XDG_CACHE_HOME=/app/xdg-cache-seed \
    python3 -c "import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt; plt.figure(); plt.close('all')"

EXPOSE 8787

# Set the process umask before Python opens the SQLite database, WAL or job
# artifacts.  "$@" preserves the existing Docker CMD/Compose command contract.
ENTRYPOINT ["/usr/bin/tini", "--", "/bin/sh", "-c", "umask 077; exec python3 /app/start_stop_service.py \"$@\"", "start-stop-service"]
CMD ["--container-mode", "--bind", "0.0.0.0", "--port", "8787", "--public-host", "127.0.0.1", "--public-port", "8787", "--database", "/app/state/database/start-stop.sqlite3", "--analysis-dir", "/app/state/published-cache/current", "--analysis-script", "/app/start_stop_analysis/analyze_and_plot_start_stop.py", "--collection-config", "/app/docker/collection-config.json", "--scratch-dir", "/app/scratch"]
