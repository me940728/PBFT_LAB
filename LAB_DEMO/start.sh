#!/bin/bash

# 반복 횟수 정의 drone_info 에 있는 전체 드론노드 수와 맟게 반복해야 함
REPEAT_COUNT=7
CONFIG_FILE="drone_info.yaml"

# 반복 횟수만큼 Python 스크립트를 비동기로 실행(순서 보장 x 먼저 생성된 p가 먼저 로그 찍힘)
for i in $(seq 0 $(($REPEAT_COUNT-1))); do
  python drone.py $i $CONFIG_FILE &
done

wait  # 모든 백그라운드 프로세스가 종료될 때까지 대기
