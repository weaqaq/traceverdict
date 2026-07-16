FROM python@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir "swebench[datasets]==4.1.0"

ENV HF_HOME=/cache/huggingface \
    HF_DATASETS_CACHE=/cache/huggingface/datasets \
    PYTHONPATH=/workspace/src \
    TMPDIR=/private/tmp

WORKDIR /workspace
