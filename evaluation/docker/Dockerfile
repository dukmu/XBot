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
    && install -d -o xbot -g xbot /opt/xbot

USER xbot
ENV HOME=/home/xbot \
    PYTHONUTF8=1 \
    UV_DEFAULT_INDEX=${PIP_MIRROR}

RUN uv python install 3.12 \
    && uv venv --python 3.12 --seed /home/xbot/.venv

# pip mirror
RUN /home/xbot/.venv/bin/pip config set global.index-url "${PIP_MIRROR}"

RUN mkdir -p /home/xbot/.config/opencode

WORKDIR /opt/xbot
COPY --chown=xbot:xbot . .

RUN --mount=type=cache,target=/home/xbot/.cache/pip,uid=${USER_ID},gid=${GROUP_ID} \
    /home/xbot/.venv/bin/pip install \
        --index-url "${PIP_MIRROR}" /opt/xbot /opt/xbot/evaluation

ENV PATH=/home/xbot/.venv/bin:$PATH

ENTRYPOINT ["python", "/opt/xbot/evaluation/run_harnessbench.py"]
