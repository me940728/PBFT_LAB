#!/bin/bash

# 반복 횟수 정의
REPEAT_COUNT=7
DRONE_CONFIG_FILE="drone_info.yaml"
BANDWIDTH_FILE="bandwidth_info.yaml"  # 대역폭 정보를 담은 파일

# 반복 횟수만큼 Python 스크립트를 비동기로 실행(순서 보장 x)
for i in $(seq 0 $(($REPEAT_COUNT-1))); do
  python drone.py $i $DRONE_CONFIG_FILE $BANDWIDTH_FILE &
done

wait  # 모든 백그라운드 프로세스가 종료될 때까지 대기
