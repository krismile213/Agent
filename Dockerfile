# mini_agent — 通用 agent 的 Web 前端镜像
# 设计: 密钥与数据一律不进镜像层 —— config.json 运行时挂载(见 .dockerignore),
#       写沙箱/会话/索引/简报全部走卷。
# 构建参数(国内网络示例):
#   docker build --build-arg BASE=docker.m.daocloud.io/library/python:3.12-slim \
#                --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple -t mini-agent:dev .
ARG BASE=python:3.12-slim
FROM ${BASE}

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

ARG PIP_INDEX=https://pypi.org/simple
COPY requirements.txt .
RUN pip install -r requirements.txt -i ${PIP_INDEX}

COPY . .
RUN mkdir -p sandbox

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/api/sessions', timeout=4)" || exit 1

CMD ["sh", "-c", "python server.py --host 0.0.0.0 --port ${PORT:-8765} --no-open --cwd /app/sandbox"]
