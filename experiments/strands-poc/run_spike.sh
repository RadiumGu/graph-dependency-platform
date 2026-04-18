#!/bin/bash
cd /home/ubuntu/tech/graph-dependency-platform/experiments/strands-poc
source .venv/bin/activate
export NEPTUNE_ENDPOINT="${NEPTUNE_ENDPOINT:-petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com}"
python spike.py 2>&1
