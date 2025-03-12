#!/bin/bash
# start.sh
# win=LF, MAC=CRLF

# Python 표준 입출력을 UTF-8로 강제
export PYTHONIOENCODING=utf-8

# 고정된 Python 인터프리터 경로 (Git Bash에서는 "/d/" 형식을 사용)
PY_CMD="/mnt/d/anaconda/envs/PBFT/python.exe" # Win OS Conda 사용
# PY_CMD="Python" # Mac OS 사용

# 드론 설정 파일 및 대역폭 파일
DRONE_CONFIG_FILE="drone_info_control_200.yaml"
BANDWIDTH_FILE="bandwidth_info.yaml"

# protocol 값 추출
PROTOCOL=$($PY_CMD -c "import yaml; print(yaml.safe_load(open('$DRONE_CONFIG_FILE'))['protocol'])" | tr -d '\r\n')
echo "사용할 프로토콜: $PROTOCOL"

# protocol 값에 따라 실행할 Python 스크립트 선택
if [ "$PROTOCOL" = "pbft" ]; then
    PYTHON_SCRIPT="pbft.py"
# 위도/경도/고도 클러스터링
elif [ "$PROTOCOL" = "lla_pbft" ]; then
    PYTHON_SCRIPT="lla_pbft.py"
# 랜덤 클러스터링
elif [ "$PROTOCOL" = "random_pbft" ]; then
    PYTHON_SCRIPT="random_pbft.py"
# 위도/경도 클러스터링
elif [ "$PROTOCOL" = "ll_pbft" ]; then
    PYTHON_SCRIPT="ll_pbft.py"
else
    echo "알 수 없는 프로토콜: $PROTOCOL"
    exit 1
fi

# YAML 파일 내 드론 수 자동 계산
REPEAT_COUNT=$($PY_CMD -c "import yaml; print(len(yaml.safe_load(open('$DRONE_CONFIG_FILE'))['drones']))" | tr -d '\r\n')
echo "드론 객체 수: $REPEAT_COUNT"

# 각 드론 인덱스에 대해 선택된 스크립트를 백그라운드에서 실행
for i in $(seq 0 $(($REPEAT_COUNT - 1))); do
  $PY_CMD $PYTHON_SCRIPT --index $i --config $DRONE_CONFIG_FILE --bandwidth $BANDWIDTH_FILE &
  sleep 0.2  # 각 노드 실행 사이에 0.2초 지연 추가
done
echo "============================================== Drone Done ============================================================>"
wait
