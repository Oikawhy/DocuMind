#!/bin/bash
IMAGES=(
    "python:3.12-slim"
    "traefik:v3"
    "postgres:16-alpine"
    "redis:7-alpine"
    "minio/minio:latest"
    "temporalio/auto-setup:latest"
    "qdrant/qdrant:latest"
    "opensearchproject/opensearch:2"
    "neo4j:5-enterprise"
    "quay.io/openbao/openbao:latest"
    "clamav/clamav:latest"
    "langfuse/langfuse:latest"
    "clickhouse/clickhouse-server:latest"
    "otel/opentelemetry-collector:latest"
    "prom/prometheus:latest"
    "grafana/grafana:latest"
    "busybox:latest"
    "ghcr.io/berriai/litellm:main-latest"
    "vllm/vllm-openai:latest"
    "grafana/loki:3.0.0"
    "grafana/tempo:2.4.1"
    "envoyproxy/envoy:v1.30-latest"
)

for img in "${IMAGES[@]}"; do
    digest=$(docker buildx imagetools inspect "$img" 2>/dev/null | grep -E '^Digest: ' | awk '{print $2}' | head -n 1)
    if [ -z "$digest" ]; then
        echo "FAILED to get digest for $img"
    else
        base="${img%%:*}"
        echo "$base@$digest"
    fi
done
