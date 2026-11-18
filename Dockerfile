FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache \
    git \
    openssh-client \
    curl \
    jq \
    ca-certificates \
    tzdata \
    findutils

RUN pip install --no-cache-dir pyyaml

# Install static kubectl binary matching target architecture
ARG TARGETARCH
RUN curl -LO "https://dl.k8s.io/release/v1.33.0/bin/linux/${TARGETARCH}/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/ \
    && kubectl version --client

WORKDIR /app

COPY entrypoint.sh /app/entrypoint.sh
COPY src/ /app/src/

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
