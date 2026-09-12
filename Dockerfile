FROM python:3.12-slim

WORKDIR /app

# 可通过 --build-arg PIP_INDEX_URL=... 覆盖 pip 源（默认使用清华镜像，国内更快）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# 先安装依赖，充分利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=100 --retries=5 \
    -i ${PIP_INDEX_URL} -r requirements.txt

# 只复制运行所需源码
COPY app.py config.py db.py ./
COPY engine ./engine
COPY static ./static

EXPOSE 8000

CMD ["python", "app.py"]
