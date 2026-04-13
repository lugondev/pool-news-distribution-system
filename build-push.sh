#!/bin/bash
set -e

# Configuration
IMAGE_NAME="news-aggregator"
REGISTRY_HOST="localhost:5001"
TAG="${1:-latest}"

echo "======================================"
echo "Building and pushing Docker image"
echo "======================================"
echo "Image: ${IMAGE_NAME}"
echo "Registry: ${REGISTRY_HOST}"
echo "Tag: ${TAG}"
echo "======================================"

# Step 1: Build the Docker image
echo ""
echo "[1/3] Building Docker image..."
docker build -t ${IMAGE_NAME}:${TAG} .

# Step 2: Tag for local registry
echo ""
echo "[2/3] Tagging image for local registry..."
docker tag ${IMAGE_NAME}:${TAG} ${REGISTRY_HOST}/${IMAGE_NAME}:${TAG}

# Step 3: Push to local registry
echo ""
echo "[3/3] Pushing to local registry..."
docker push ${REGISTRY_HOST}/${IMAGE_NAME}:${TAG}

echo ""
echo "======================================"
echo "✅ Build and push completed successfully!"
echo "======================================"
echo "Image: ${REGISTRY_HOST}/${IMAGE_NAME}:${TAG}"
echo ""
echo "To pull the image:"
echo "  docker pull ${REGISTRY_HOST}/${IMAGE_NAME}:${TAG}"
echo ""
echo "To run the container:"
echo "  docker run -d -p 8000:8000 ${REGISTRY_HOST}/${IMAGE_NAME}:${TAG}"
echo "======================================"
