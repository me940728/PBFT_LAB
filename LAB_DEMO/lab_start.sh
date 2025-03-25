#!/bin/bash
# lab_start.sh

# 배열 변수 설정
#PROTOCOLS=("pbft" "lla_pbft" "random_pbft")
PROTOCOLS=("lla_pbft" "random_pbft")
COVERAGE_RADIUS_VALUES=("50" "100" "150" "200" "250" "299")
DRONE_VALUES=("20" "40" "60" "80")
ROUNDS=10

BANDWIDTH_FILE="bandwidth_info.yaml"
LOG_DIR="lab_log"   # 로그 파일이 저장될 디렉토리

# Python 표준 입출력을 UTF-8로 강제
export PYTHONIOENCODING=utf-8
PY_CMD="python"

# 로그 디렉토리 생성
mkdir -p "$LOG_DIR"

# PROTOCOL 배열 순회
for proto in "${PROTOCOLS[@]}"; do
    echo "-------------------------------"
    echo "현재 프로토콜: $proto"
    # 프로토콜 별로 결과 파일 초기화 (기존 파일 삭제)
    CONSENSUS_TIME_FILE="${LOG_DIR}/${proto}_consensustime.yaml"
    CONSENSUS_TIME_META_FILE="${LOG_DIR}/${proto}_consensustime_meta.yaml"
    rm -f "$CONSENSUS_TIME_FILE" "$CONSENSUS_TIME_META_FILE"

    # COVERAGE_RADIUS 배열 순회
    for coverage in "${COVERAGE_RADIUS_VALUES[@]}"; do
        # DRONE 배열 순회
        for drone in "${DRONE_VALUES[@]}"; do
            # ROUNDS 반복
            for round in $(seq 1 $ROUNDS); do
                echo "-----------------------------------------------"
                echo "Protocol: $proto, Coverage: ${coverage}m, Drone: ${drone}d, Round: $round"
                
                # 동적으로 파라미터 파일 경로 생성
                DRONE_CONFIG_FILE="parameter_files/${proto}/${coverage}/drone_info_control_${drone}.yaml"
                
                # 설정 파일에서 protocol 값 추출 (config에 명시된 값)
                current_proto=$($PY_CMD -c "import yaml; print(yaml.safe_load(open('$DRONE_CONFIG_FILE'))['protocol'])" | tr -d '\r\n')
                echo "설정 파일의 프로토콜: $current_proto"
                
                # 프로토콜에 따라 실행할 Python 스크립트 선택
                if [ "$current_proto" = "pbft" ]; then
                    PYTHON_SCRIPT="pbft.py"
                elif [ "$current_proto" = "lla_pbft" ]; then
                    PYTHON_SCRIPT="lla_pbft.py"
                elif [ "$current_proto" = "random_pbft" ]; then
                    PYTHON_SCRIPT="random_pbft.py"
                elif [ "$current_proto" = "ll_pbft" ]; then
                    PYTHON_SCRIPT="ll_pbft.py"
                else
                    echo "알 수 없는 프로토콜: $current_proto"
                    exit 1
                fi

                # YAML 파일 내 드론 수 자동 계산
                REPEAT_COUNT=$($PY_CMD -c "import yaml; print(len(yaml.safe_load(open('$DRONE_CONFIG_FILE'))['drones']))" | tr -d '\r\n')
                echo "드론 객체 수: $REPEAT_COUNT"
                
                # 각 드론 인덱스에 대해 선택된 스크립트를 nohup으로 백그라운드 실행 (데몬화)
                for i in $(seq 0 $(($REPEAT_COUNT - 1))); do
                    nohup $PY_CMD $PYTHON_SCRIPT --index $i --config $DRONE_CONFIG_FILE --bandwidth $BANDWIDTH_FILE > /dev/null 2>&1 &
                    sleep 0.2  # 각 노드 실행 사이 0.2초 지연
                done
                echo "============================================== Drone Started ============================================================>"
                # 충분한 기동 대기 시간
                sleep 2

                # curl 요청 수행 (여기서는 포트 20001 사용; 필요 시 수정)
                echo "curl 요청 시작..."
                CURL_RESPONSE=$(curl -s --max-time 600 -X POST http://localhost:20001/start-protocol \
                    -H "Content-Type: application/json" \
                    -d '{"latitude": 36.6261519, "longitude": 127.4590123, "altitude": 0}')
                echo "===== CURL 응답 상세 내용 ====="
                echo "$CURL_RESPONSE"
                echo "==============================="

                # 메타 응답 데이터를 그대로 저장 (append)
                echo "$CURL_RESPONSE" >> "$CONSENSUS_TIME_META_FILE"
                echo "응답 데이터가 $CONSENSUS_TIME_META_FILE 에 저장되었습니다."

                # 유효한 JSON 데이터만 추출 (줄의 시작과 끝이 { } 인 줄)
                JSON_RESPONSE=$(echo "$CURL_RESPONSE" | grep -E '^\{.*\}$')
                echo ">>>>>>>>>>>>>>>>>>> $JSON_RESPONSE"

                # JSON 응답에서 average_time 추출 (Python 사용)
                avg_time=$(python -c "import sys, json; print(json.load(sys.stdin)['protocol']['average_time'])" <<< "$JSON_RESPONSE")
                echo "추출된 average_time: $avg_time"

                # JSON 파일 업데이트 (jq 없이 Python here-document 사용)
                python <<EOF
import json, os
file_path = "$CONSENSUS_TIME_FILE"
coverage_key = "${coverage}m"
drone_key = "${drone}d"
new_value = $avg_time
if os.path.exists(file_path):
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
    except Exception:
        data = {}
else:
    data = {}
if coverage_key not in data:
    data[coverage_key] = {}
if drone_key not in data[coverage_key]:
    data[coverage_key][drone_key] = []
data[coverage_key][drone_key].append(new_value)
with open(file_path, 'w') as f:
    json.dump(data, f, indent=4)
EOF
                echo "구조화된 consensustime 데이터가 $CONSENSUS_TIME_FILE 에 저장되었습니다."

                # 모든 작업이 완료된 후 kill_drone.sh 실행하여 노드 종료
                echo "모든 작업이 완료되었습니다. kill_drone.sh 파일을 실행합니다."
                ./kill_drone.sh "$DRONE_CONFIG_FILE"

                # 다음 라운드 전 잠시 대기
                sleep 1
            done  # end ROUNDS loop
        done  # end DRONE loop
    done  # end COVERAGE_RADIUS loop
done  # end PROTOCOL loop
