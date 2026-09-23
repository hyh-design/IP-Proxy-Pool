FROM ip-proxy-pool:local
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/sh proxy-peer \
    && passwd -d proxy-peer \
    && mkdir -p /run/sshd
