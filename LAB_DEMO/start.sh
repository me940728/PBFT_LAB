#!/bin/bash
# start.sh

# 드론 설정 파일 및 대역폭 파일
DRONE_CONFIG_FILE="drone_info_control_0.yaml"
BANDWIDTH_FILE="bandwidth_info.yaml"

# drone_info_control.yaml에서 protocol 값 추출
PROTOCOL=$(python -c "import yaml; print(yaml.safe_load(open('$DRONE_CONFIG_FILE'))['protocol'])")
echo "사용할 프로토콜: $PROTOCOL"

# protocol 값에 따라 실행할 Python 스크립트 선택
if [ "$PROTOCOL" = "pbft" ]; then
    PYTHON_SCRIPT="pbft.py"
elif [ "$PROTOCOL" = "llapbft" ]; then
    PYTHON_SCRIPT="lla_pbft.py"
elif [ "$PROTOCOL" = "random" ]; then
    PYTHON_SCRIPT="random_pbft.py"
else
    echo "알 수 없는 프로토콜: $PROTOCOL"
    exit 1
fi

# YAML 파일 내 드론 수 자동 계산
REPEAT_COUNT=$(python -c "import yaml; print(len(yaml.safe_load(open('$DRONE_CONFIG_FILE'))['drones']))")
echo "드론 객체 수: $REPEAT_COUNT"

# 각 드론 인덱스(0부터 REPEAT_COUNT-1까지)에 대해 선택된 스크립트를 백그라운드에서 실행
for i in $(seq 0 $(($REPEAT_COUNT - 1))); do
  python $PYTHON_SCRIPT --index $i --config $DRONE_CONFIG_FILE --bandwidth $BANDWIDTH_FILE &
done

wait