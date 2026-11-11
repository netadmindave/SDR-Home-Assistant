# RTL-SDR Survey: rtl_433 + rtl_power + radiosonde_auto_rx time-sliced on one dongle, MQTT + HA discovery.
# Works as a Home Assistant add-on or as a plain container.
# The runtime stage is pinned to Debian on purpose: Supervisor's default BUILD_FROM is the
# Alpine-based HA base image, and every stage here needs apt + glibc.

# ---------------------------------------------------------------- C tools
FROM debian:bookworm-slim AS cbuild
ARG RTLSDR_REF=v1.3.6
# rtl_433 development branch: ~100 more decoders than the 25.12 release (US car remotes, TPMS,
# garage openers...). Pin a release tag (e.g. --build-arg RTL433_REF=25.12) if master breaks.
ARG RTL433_REF=master
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git pkg-config libusb-1.0-0-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# rtl-sdr-blog fork of librtlsdr: same API, adds RTL-SDR Blog V4 (R828D) support
RUN git clone --depth 1 --branch ${RTLSDR_REF} https://github.com/rtlsdrblog/rtl-sdr-blog.git /src/rtl-sdr \
    && cmake -S /src/rtl-sdr -B /src/rtl-sdr/build -DCMAKE_BUILD_TYPE=Release \
         -DINSTALL_UDEV_RULES=OFF -DDETACH_KERNEL_DRIVER=ON -DCMAKE_INSTALL_PREFIX=/usr/local \
    && cmake --build /src/rtl-sdr/build -j"$(nproc)" \
    && cmake --install /src/rtl-sdr/build
RUN git clone --depth 1 --branch ${RTL433_REF} https://github.com/merbanan/rtl_433.git /src/rtl_433 \
    && PKG_CONFIG_PATH=/usr/local/lib/pkgconfig cmake -S /src/rtl_433 -B /src/rtl_433/build \
         -DCMAKE_BUILD_TYPE=Release -DENABLE_RTLSDR=ON -DENABLE_SOAPYSDR=OFF -DENABLE_OPENSSL=OFF \
         -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_PREFIX_PATH=/usr/local \
    && cmake --build /src/rtl_433/build -j"$(nproc)" \
    && cmake --install /src/rtl_433/build

# ---------------------------------------------------------------- radiosonde_auto_rx
FROM debian:bookworm-slim AS autorx
# Pinned commit (auto_rx 1.9.0). Override with --build-arg AUTORX_REF=master for the latest.
ARG AUTORX_REF=53d03c72ad18ce4357c0cedd1f4acf2bf1efb36e
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git pkg-config libfftw3-dev ca-certificates python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/projecthorus/radiosonde_auto_rx.git /src/autorx \
    && cd /src/autorx && git checkout ${AUTORX_REF} \
    && cd auto_rx && bash build.sh \
    && mkdir -p /opt && cp -r /src/autorx/auto_rx /opt/auto_rx && cp /src/autorx/LICENSE /opt/auto_rx/
RUN python3 -m venv /opt/autorx-venv \
    && /opt/autorx-venv/bin/pip install --no-cache-dir -r /opt/auto_rx/requirements.txt

# ---------------------------------------------------------------- runtime
FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        libusb-1.0-0 libfftw3-single3 sox python3 python3-aiohttp python3-paho-mqtt python3-yaml ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=cbuild /usr/local/lib/ /usr/local/lib/
COPY --from=cbuild /usr/local/bin/rtl_433 /usr/local/bin/rtl_power /usr/local/bin/rtl_fm \
                   /usr/local/bin/rtl_sdr /usr/local/bin/rtl_test /usr/local/bin/rtl_eeprom /usr/local/bin/
COPY --from=autorx /opt/auto_rx /opt/auto_rx
COPY --from=autorx /opt/autorx-venv /opt/autorx-venv
RUN ldconfig && rtl_433 -V 2>&1 | head -1 && test -x /opt/auto_rx/rs41mod

COPY app /app
WORKDIR /app
EXPOSE 8099 5000
VOLUME ["/data"]
CMD ["python3", "/app/main.py"]
