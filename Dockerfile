FROM python:3.12-slim

WORKDIR /app
# pip 源做成 build arg：国内宿主 compose 里默认传阿里云源（快）；
# CI 在 GitHub 机房跑，默认 pypi.org 官方源——走阿里云反而慢到超时（run #6 实测）。
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    HF_ENDPOINT=https://hf-mirror.com

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8765 8501
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
