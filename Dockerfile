FROM python:3.8.10-slim-buster
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANSIBLE_HOST_KEY_CHECKING=True

WORKDIR /app
RUN printf 'deb http://archive.debian.org/debian buster main\n' > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
    git openssh-client ca-certificates build-essential libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /root/.ssh && chmod 700 /root/.ssh

# Inventory may pin localhost to /usr/bin/python3; use the same Python as pip.
RUN ln -sfn /usr/local/bin/python3 /usr/bin/python3

COPY requirements.txt ansible-collections.yml ./
RUN python -m pip install --no-cache-dir 'pip<25' \
    && /usr/bin/python3 -m pip install --no-cache-dir -r requirements.txt 'python-gitlab==3.15.0' \
    && for attempt in 1 2 3; do \
         if ansible-galaxy collection install -r ansible-collections.yml -p /usr/share/ansible/collections; then \
           break; \
         fi; \
         if [ "$attempt" -eq 3 ]; then \
           echo "Ansible Galaxy failed after 3 attempts; check the CA chain or proxy." >&2; \
           exit 1; \
         fi; \
         sleep 5; \
       done \
    && /usr/bin/python3 -c "import sys, jinja2, gitlab; assert sys.version_info[:3] == (3, 8, 10); assert jinja2.__version__ == '2.10.1'" \
    && ansible --version | grep -F 'ansible [core 2.12.10]'

COPY config.py git_auth.py runtime.py workflow.py bot.py ./
COPY bin/run ./bin/run

RUN chmod +x ./bin/run

EXPOSE 8080

CMD ["/app/bin/run"]
