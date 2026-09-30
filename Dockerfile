FROM python:3.8.10-slim-bullseye

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANSIBLE_HOST_KEY_CHECKING=True

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    git openssh-client ca-certificates build-essential libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /root/.ssh && chmod 700 /root/.ssh

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir 'pip<25' \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -c "import sys, jinja2; assert sys.version_info[:3] == (3, 8, 10); assert jinja2.__version__ == '2.10.1'" \
    && ansible --version | grep -F 'ansible [core 2.12.10]'

COPY config.py workflow.py bot.py ./

RUN chmod +x bin/run
CMD ["python", "bot.py"]
