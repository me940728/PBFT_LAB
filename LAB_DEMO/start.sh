#!/bin/bash

# 반복 횟수 정의 (예: 6번 반복)
REPEAT_COUNT=7
CONFIG_FILE="drone_info.yaml"

# 반복 횟수만큼 Python 스크립트 실행
for i in $(seq 0 $(($REPEAT_COUNT-1))); do
  python drone.py $i $CONFIG_FILE &
done

wait  # 모든 백그라운드 프로세스가 종료될 때까지 대기
