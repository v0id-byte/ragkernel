# CPU-only 镜像：显式装 CPU 版 torch，避免 sentence-transformers 拉进 CUDA 巨型依赖。
# 模型不烤进镜像——首启由 entrypoint 拉到挂载的 /models 卷（约 2GB，仅一次）。
FROM python:3.12-slim

WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models \
    SENTENCE_TRANSFORMERS_HOME=/models \
    RAGKERNEL_HOST=0.0.0.0

# 先装 CPU 版 torch（锁定 CPU wheel 源），再装本项目其余依赖
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY . .
RUN pip install -e .

EXPOSE 8360
ENTRYPOINT ["/app/docker/entrypoint.sh"]
