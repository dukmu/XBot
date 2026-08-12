FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG USER_ID=1000
ARG GROUP_ID=1000
ARG OPENCODE_VERSION=1.18.15
ARG APT_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/ubuntu
ARG PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple

RUN sed -i \
        -e "s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR}|g" \
        -e "s|http://security.ubuntu.com/ubuntu|${APT_MIRROR}|g" \
        /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        bubblewrap \
        ca-certificates \
        curl \
        file \
        git \
        imagemagick \
        jq \
        procps \
        ripgrep \
        sqlite3 \
        unzip \
        xz-utils \
        zip \
        zsh \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/nodesource.sh \
    && bash /tmp/nodesource.sh \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* /tmp/nodesource.sh \
    && npm install --global "opencode-ai@${OPENCODE_VERSION}"

RUN curl -LsSf https://astral.sh/uv/install.sh -o /tmp/install-uv.sh \
    && env UV_INSTALL_DIR=/usr/local/bin sh /tmp/install-uv.sh \
    && rm /tmp/install-uv.sh

RUN groupadd --gid "${GROUP_ID}" xbot \
    && useradd --create-home --uid "${USER_ID}" --gid "${GROUP_ID}" xbot \
    && install -d -o xbot -g xbot /opt/xbot /opt/uv-python /opt/venv

USER xbot
ENV HOME=/home/xbot \
    PIP_INDEX_URL=${PIP_MIRROR} \
    PYTHONUTF8=1 \
    UV_DEFAULT_INDEX=${PIP_MIRROR} \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python

RUN uv python install 3.12 \
    && uv venv --python 3.12 --seed /opt/venv

WORKDIR /opt/xbot

USER root
RUN --mount=type=bind,target=/opt/xbot,rw \
    --mount=type=cache,target=/home/xbot/.cache/pip \
    /opt/venv/bin/pip install \
        --index-url "${PIP_MIRROR}" \
        -e /opt/xbot -e /opt/xbot/evaluation \
        numpy Pillow opencv-python-headless
USER xbot

ENV PATH=/opt/venv/bin:$PATH

ENTRYPOINT ["python", "/opt/xbot/evaluation/run_harnessbench.py"]
