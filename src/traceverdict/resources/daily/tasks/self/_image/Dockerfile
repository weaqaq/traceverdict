FROM python@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

COPY requirements.txt /tmp/traceverdict-self-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/traceverdict-self-requirements.txt

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /testbed
