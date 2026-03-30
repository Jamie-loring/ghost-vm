FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:1
ENV SCREEN_RESOLUTION=1920x1080x24
ENV LANG=en_US.UTF-8
ENV LANGUAGE=en_US:en
ENV LC_ALL=en_US.UTF-8
ENV TZ=America/New_York
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

RUN apt-get update && apt-get install -y \
    # Virtual display & desktop
    xvfb x11vnc openbox xterm dbus-x11 \
    # noVNC web client
    novnc websockify \
    # Fonts — broad stack to defeat font-enumeration fingerprinting
    fonts-liberation fonts-liberation2 \
    fonts-noto fonts-noto-color-emoji fonts-noto-cjk \
    fonts-dejavu fonts-dejavu-core fonts-freefont-ttf \
    fonts-ubuntu fonts-croscore \
    fonts-crosextra-carlito fonts-crosextra-caladea \
    ttf-bitstream-vera xfonts-base xfonts-75dpi \
    # Python
    python3 python3-pip python3-dev \
    # Misc
    curl wget ca-certificates locales tzdata \
    xdotool scrot \
    && locale-gen en_US.UTF-8 \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

# MS core fonts (Arial, Verdana, Georgia, Times New Roman, etc.)
# Pre-accept EULA so install is non-interactive; fonts fetched from SourceForge at build time.
RUN echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" \
        | debconf-set-selections \
    && apt-get update \
    && apt-get install -y --no-install-recommends ttf-mscorefonts-installer \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user (avoids --no-sandbox detection signal)
RUN useradd -m -s /bin/bash user && usermod -aG audio,video user

# Python automation stack
COPY automation/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# Playwright browser + all system deps it needs
RUN PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers playwright install chromium
RUN PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers playwright install-deps chromium

# Browser profile directory with realistic prefs baked in
RUN mkdir -p /home/user/.config/chromium/Default \
    && chown -R user:user /home/user/.config

COPY automation/chrome_prefs.json /home/user/.config/chromium/Default/Preferences
COPY automation/ /app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && chown -R user:user /app

WORKDIR /app

# noVNC web UI, raw VNC, automation API
EXPOSE 6080 5900 8080

ENTRYPOINT ["/entrypoint.sh"]
