ARG BUILD_ARCH=amd64

# ===========================================================================
# Stage 1 — Build shairport-sync from source with embedded tinysvcmdns.
#
# The Alpine community package links against Avahi, which requires a D-Bus
# system bus for mDNS.  With host_network: true the host already runs
# avahi-daemon on port 5353, and the D-Bus security policy refuses to let
# the container register Avahi services.  Every attempt to work around this
# (private session bus, private system bus, shared host Avahi) failed.
#
# Compiling with --with-tinysvcmdns embeds the mDNS responder directly into
# shairport-sync — it broadcasts UDP multicast on port 5353 itself, with no
# external daemon and no D-Bus dependency.  Multiple mDNS responders can
# coexist on the same port (SO_REUSEADDR), so this works even when the host
# also runs avahi-daemon.
# ===========================================================================
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:latest AS builder

RUN apk add --no-cache \
        alpine-sdk \
        autoconf \
        automake \
        libtool \
        pkgconf \
        git \
        alsa-lib-dev \
        pulseaudio-dev \
        pipewire-dev \
        libconfig-dev \
        popt-dev \
        libdaemon-dev \
        soxr-dev \
        openssl-dev \
        dbus-dev

WORKDIR /tmp

# Build the Apple ALAC decoder library from source.
# The Hammerton decoder (shairport-sync's default) crashes (SIGSEGV) on
# modern iOS ALAC streams that use prediction type 15. The Apple ALAC
# decoder handles all prediction types correctly.
RUN git clone --depth=1 https://github.com/mikebrady/ALAC.git /tmp/alac \
    && cd /tmp/alac \
    && autoreconf -fi \
    && ./configure --prefix=/usr/local \
    && make -j$(nproc) \
    && make install DESTDIR=/tmp/alac-install

RUN git clone --depth=1 https://github.com/mikebrady/shairport-sync.git /tmp/shairport-sync

WORKDIR /tmp/shairport-sync

RUN autoreconf -fi \
    && PKG_CONFIG_PATH="/tmp/alac-install/usr/local/lib/pkgconfig" \
       CFLAGS="-I/tmp/alac-install/usr/local/include" \
       CXXFLAGS="-I/tmp/alac-install/usr/local/include" \
       LDFLAGS="-L/tmp/alac-install/usr/local/lib" \
       ./configure \
        --with-alsa \
        --with-pulseaudio \
        --with-soxr \
        --with-ssl=openssl \
        --with-tinysvcmdns \
        --with-metadata \
        --with-dbus-interface \
        --with-apple-alac \
        --sysconfdir=/etc \
        --prefix=/usr \
    && make -j$(nproc) \
    && make install DESTDIR=/tmp/install

# Ensure /tmp/install/usr/lib exists, then copy the Apple ALAC library (static or shared).
RUN mkdir -p /tmp/install/usr/lib \
    && cp /tmp/alac-install/usr/local/lib/libalac.a /tmp/install/usr/lib/ 2>/dev/null || true \
    && cp /tmp/alac-install/usr/local/lib/libalac.so* /tmp/install/usr/lib/ 2>/dev/null || true

# ===========================================================================
# Stage 2 — Runtime image.
# ===========================================================================
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:latest

# Copy the compiled shairport-sync binary, Apple ALAC library, and config.
COPY --from=builder /tmp/install/usr/bin/shairport-sync /usr/bin/shairport-sync
COPY --from=builder /tmp/install/usr/lib/ /usr/lib/
COPY --from=builder /tmp/install/etc/shairport-sync.conf.sample /etc/shairport-sync.conf.sample

RUN ldconfig /usr/lib 2>/dev/null || true

# Install runtime dependencies (no Avahi or avahi-compat-libdns_sd needed).
RUN apk add --no-cache \
        bluez \
        bluez-deprecated \
        bluez-libs \
        bluez-openrc \
        pipewire \
        pipewire-pulse \
        pipewire-tools \
        pipewire-spa-bluez \
        wireplumber \
        pulseaudio-alsa \
        pulseaudio-utils \
        python3 \
        py3-pip \
        py3-dbus \
        py3-gobject3 \
        py3-requests \
        py3-flask \
        dbus \
        dbus-libs \
        dbus-glib \
        jq \
        curl \
        socat \
        alsa-utils \
        alsa-plugins-pulse \
        libconfig \
        popt \
        libdaemon \
        soxr \
        openssl \
        util-linux \
        procps-ng \
        coreutils \
        findutils \
        bash

# Python dependencies.
COPY requirements.txt /tmp/requirements.txt

RUN pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && rm -rf /var/cache/apk/* /tmp/requirements.txt

# Copy the add-on root filesystem overlay.
COPY rootfs /

# Make scripts executable.
RUN chmod a+x /usr/share/alexa-airplay-bridge/run.py \
    && chmod a+x /usr/share/alexa-airplay-bridge/entrypoint.sh \
    && chmod a+x /usr/share/alexa-airplay-bridge/audio_hook.sh \
    && chmod a+x /usr/share/alexa-airplay-bridge/bt_switch.sh \
    && chmod -R a+rX /usr/share/alexa-airplay-bridge

WORKDIR /usr/share/alexa-airplay-bridge

LABEL io.hass.name="AirPlay to Bluetooth Bridge"
LABEL io.hass.description="Bridge AirPlay audio to Amazon Echo Bluetooth speakers"
LABEL io.hass.arch="amd64,aarch64"
LABEL io.hass.type="addon"
LABEL io.hass.version="2.0.47"

CMD ["/usr/share/alexa-airplay-bridge/entrypoint.sh"]
