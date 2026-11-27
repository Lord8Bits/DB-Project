#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="yt-vitess"

echo "Waiting for VTGate on port 15003..."
until docker exec "$CONTAINER_NAME" mysql -h 127.0.0.1 -P 15003 -u root -e "SELECT 1" >/dev/null 2>&1; do
  sleep 2
done

echo "Applying SQL schema for yt_core..."
docker exec -i "$CONTAINER_NAME" mysql -h 127.0.0.1 -P 15003 -u root < schemas/yt_core_schema.sql

echo "Applying VSchema for yt_core..."
docker exec -i "$CONTAINER_NAME" vtctldclient --server=127.0.0.1:15001 ApplyVSchema \
  --vschema="$(cat schemas/yt_core_vschema.json)" yt_core

echo "Applying SQL schema for yt_videos (across shards -80 and 80-)..."
docker exec -i "$CONTAINER_NAME" mysql -h 127.0.0.1 -P 15003 -u root < schemas/yt_videos_schema.sql

echo "Applying VSchema for yt_videos..."
docker exec -i "$CONTAINER_NAME" vtctldclient --server=127.0.0.1:15001 ApplyVSchema \
  --vschema="$(cat schemas/yt_videos_vschema.json)" yt_videos

echo "Vitess initialization complete!"