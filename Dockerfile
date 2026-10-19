FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache \
    git \
    openssh-client \
    curl \
    jq \
    ca-certificates \
    tzdata

RUN pip install --no-cache-dir pyyaml

# Install static kubectl binary
ARG TARGETARCH=amd64
RUN curl -LO "https://dl.k8s.io/release/v1.31.0/bin/linux/${TARGETARCH}/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/

WORKDIR /app

COPY entrypoint.sh /app/entrypoint.sh
COPY src/ /app/src/

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]

