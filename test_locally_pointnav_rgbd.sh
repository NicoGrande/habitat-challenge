#!/usr/bin/env bash

DOCKER_NAME="pointnav_submission"

while [[ $# -gt 0 ]]
do
key="${1}"

case $key in
      --docker-name)
      shift
      DOCKER_NAME="${1}"
	  shift
      ;;
    *) # unknown arg
      echo unkown arg ${1}
      exit
      ;;
esac
done

docker run -v $(pwd)/habitat-challenge-data:/habitat-challenge-data \
    -v $(realpath habitat-challenge-data/data/scene_datasets/gibson):$(realpath habitat-challenge-data/data/scene_datasets/gibson) \
    --runtime=nvidia \
    -e "AGENT_EVALUATION_TYPE=local" \
    -e "TRACK_CONFIG_FILE=/challenge_pointnav2020.local.rgbd.yaml" \
    ${DOCKER_NAME}\

