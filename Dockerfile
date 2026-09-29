FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    git \
    ssh \
    curl \
    unzip \
    vim \
    tesseract-ocr \
    tesseract-ocr-vie \
    && \
    # Install terraform
    curl -Lo /tmp/terraform.zip https://releases.hashicorp.com/terraform/1.9.0/terraform_1.9.0_linux_amd64.zip \
    && unzip /tmp/terraform.zip -d /usr/local/bin/ \
    && rm /tmp/terraform.zip \
    && \
    # Install terragrunt
    curl -Lo /usr/local/bin/terragrunt \
       https://github.com/gruntwork-io/terragrunt/releases/download/v0.67.0/terragrunt_linux_amd64 \
    && chmod +x /usr/local/bin/terragrunt \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && echo "alias ll='ls -alF'" >> /etc/bash.bashrc

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]