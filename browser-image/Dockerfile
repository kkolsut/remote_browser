# Dual-browser Selkies image: Google Chrome Stable + Mozilla Firefox + browser chooser (plan §20-24).
# Build with scripts/build-browser-image.sh, which tags the image with the date and browser versions.
# Classic-builder compatible (no BuildKit-only syntax).
#
# The base is pinned by tag and digest; bump it deliberately (plan §54). Chrome and Firefox are
# installed fresh at build time, so a rebuild (--pull --no-cache) picks up browser security fixes.
ARG SELKIES_BASE=ghcr.io/linuxserver/baseimage-selkies:debiantrixie-3900c0f0-ls140@sha256:c3f25149cf58957fcc4259904b02cf88b1e51fc8df2f9946bc64650701e527e7
FROM ${SELKIES_BASE}

# Primary keys of the browser APT repositories; the build fails if a downloaded key differs.
ARG GOOGLE_KEY_FPR=EB4C1BFD4F042F6DDDCCEC917721F63BD38B4796
ARG MOZILLA_KEY_FPR=35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3

LABEL org.opencontainers.image.title="remote-dual-browser" \
      org.opencontainers.image.description="Selkies session with Google Chrome, Mozilla Firefox and a browser chooser"

# X11/openbox session (the launcher and both browsers run on Xvfb).
# HARDEN_DESKTOP: no sudo, no terminals, no xdg-open, no Selkies file transfer/command/app sidebar.
# RESTART_APP: the base image's watchdog restarts the chooser if it ever exits.
ENV TITLE="Remote Browser" \
    PIXELFLUX_WAYLAND=false \
    HARDEN_DESKTOP=true \
    RESTART_APP=true \
    START_DOCKER=false

RUN set -eux; \
    install -d -m 0755 /etc/apt/keyrings; \
    check_key() { \
        actual="$(gpg --show-keys --with-colons "$1" | awk -F: '/^pub/ { getline; print $10 }')"; \
        [ "$actual" = "$2" ] || { echo "unexpected signing key in $1: $actual" >&2; exit 1; }; \
    }; \
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub -o /tmp/google.pub; \
    check_key /tmp/google.pub "$GOOGLE_KEY_FPR"; \
    gpg --dearmor < /tmp/google.pub > /etc/apt/keyrings/google-chrome.gpg; \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list; \
    curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg -o /etc/apt/keyrings/packages.mozilla.org.asc; \
    check_key /etc/apt/keyrings/packages.mozilla.org.asc "$MOZILLA_KEY_FPR"; \
    echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
        > /etc/apt/sources.list.d/mozilla.list; \
    # Firefox only from Mozilla's repository, never Debian's firefox-esr (plan §22).
    printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n\nPackage: firefox-esr\nPin: release *\nPin-Priority: -1\n' \
        > /etc/apt/preferences.d/mozilla; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends google-chrome-stable firefox; \
    # Chrome's package installs its own repository definition and a daily cron job that rewrites it.
    rm -f /etc/cron.daily/google-chrome /etc/apt/sources.list.d/google-chrome.sources; \
    # The session user gets no way to root: no sudo, no docker group, no password (plan §31).
    SUDO_FORCE_REMOVE=yes DEBIAN_FRONTEND=noninteractive apt-get purge -y sudo; \
    rm -rf /etc/sudoers.d; \
    gpasswd -d abc sudo || true; \
    gpasswd -d abc docker || true; \
    passwd -l abc; \
    # Versions for the image tag (scripts/build-browser-image.sh).
    { echo "chrome=$(dpkg-query -W -f '${Version}' google-chrome-stable)"; \
      echo "firefox=$(dpkg-query -W -f '${Version}' firefox)"; } > /etc/remote-browser-versions; \
    cat /etc/remote-browser-versions; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/*

# Selkies data paths into the session, off unless needed (plan §43). The clipboard stays on;
# microphone forwarding is off by Selkies default. SELKIES_ALLOWED_ORIGINS (the base image sets "*")
# is set per container to the public origin, see scripts/rb_units.py.
ENV SELKIES_WEBCAM_ENABLED=false \
    SELKIES_UI_SIDEBAR_SHOW_WEBCAM=false \
    SELKIES_GAMEPAD_ENABLED=false

# The Selkies apps panel installs and runs arbitrary programs through selkies-proot; remove it
# (HARDEN_DESKTOP already denies the command channel it needs).
RUN rm -f /usr/local/bin/selkies-proot /usr/local/bin/steam /steam.sh

COPY root/ /
COPY launcher/browser-launcher.py /usr/local/bin/browser-launcher

# Selkies HTTP. Only Nginx on the user's private network reaches it (plan §29-30).
EXPOSE 3000
VOLUME /config
