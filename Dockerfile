FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    HOME=/tmp
WORKDIR /app
# RE0 直连通道：CloakBrowser（Chromium 146）运行库 + Playwright 驱动 + Ed25519 验签
# CloakBrowser 二进制不进镜像（官方许可 no redistribution），运行时经代理从 GitHub 下载到 data/
# apt 换阿里云源：deb.debian.org 在国内极慢（上次 build 卡在这 800+ 秒），换源缩短 build 时长
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
    /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
    xvfb x11vnc novnc websockify xdotool \
    fonts-noto-cjk fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ --upgrade playwright pynacl
COPY app/ /app/
RUN find /app -name "._*" -type f -delete && \
    find /app -name "*.bak" -type f -delete && \
    rm -f /app/core/symedia.py && \
    chmod -R a+rX /app
EXPOSE 9191
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9191/api/stats',timeout=4).status==200 else 1)"
CMD ["python3", "server.py"]
