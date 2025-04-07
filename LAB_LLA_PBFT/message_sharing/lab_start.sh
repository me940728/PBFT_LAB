#!/bin/bash
# lab_start.sh

# 배열 변수 설정
PROTOCOLS=("lla_pbft" "random_pbft")
#PROTOCOLS=("random_pbft")
COVERAGE_RADIUS_VALUES=("50" "100" "150" "200" "250" "299")
#COVERAGE_RADIUS_VALUES=("50" "100")
DRONE_VALUES=("40" "80" "120" "160" "200")
#DRONE_VALUES=("40" "80")
ROUNDS=10
MESSAGE_SIZE_VALUES=(1 3 5)
CLUSTER_SIZE_VALUES=(4 6 8)

BANDWIDTH_FILE="bandwidth_info.yaml"
BASE_LOG_DIR="lab_log"   # 최상위 로그 디렉토리

# Python 표준 입출력을 UTF-8로 강제
export PYTHONIOENCODING=utf-8
PY_CMD="python"

# 최상위 로그 디렉토리 생성
mkdir -p "$BASE_LOG_DIR"

# PROTOCOL 배열 순회
for proto in "${PROTOCOLS[@]}"; do
    echo "-------------------------------"
    echo "현재 프로토콜: $proto"
    
    # 메시지 크기와 클러스터 크기 루프 추가
    for m in "${MESSAGE_SIZE_VALUES[@]}"; do
        for cluster_size in "${CLUSTER_SIZE_VALUES[@]}"; do
            # 결과 파일 저장할 디렉터리 (예: lab_log/m_1/k_4)
            OUTPUT_DIR="${BASE_LOG_DIR}/m_${m}/k_${cluster_size}"
            mkdir -p "$OUTPUT_DIR"
            
            CONSENSUS_TIME_FILE="${OUTPUT_DIR}/${proto}_consensustime.yaml"
            CONSENSUS_TIME_META_FILE="${OUTPUT_DIR}/${proto}_consensustime_meta.yaml"
            rm -f "$CONSENSUS_TIME_FILE" "$CONSENSUS_TIME_META_FILE"
            
            # COVERAGE 배열 순회
            for coverage in "${COVERAGE_RADIUS_VALUES[@]}"; do
                # DRONE 배열 순회
                for drone in "${DRONE_VALUES[@]}"; do
                    for round in $(seq 1 $ROUNDS); do
                        echo "-----------------------------------------------"
                        echo "Protocol: $proto, Message Size: ${m}MB, Cluster: ${cluster_size}, Coverage: ${coverage}m, Drone: ${drone}d, Round: $round"
                        
                        # 동적으로 파라미터 파일 경로 생성
                        DRONE_CONFIG_FILE="parameter_files/${proto}/${coverage}/drone_info_control_${drone}.yaml"
                        
                        # 설정 파일에서 protocol 값 추출
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
                        
                        # YAML 파일 내 드론 수 계산
                        REPEAT_COUNT=$($PY_CMD -c "import yaml; print(len(yaml.safe_load(open('$DRONE_CONFIG_FILE'))['drones']))" | tr -d '\r\n')
                        echo "드론 객체 수: $REPEAT_COUNT"
                        
                        # 각 드론 인덱스에 대해 선택된 스크립트를 백그라운드 실행
                        for i in $(seq 0 $(($REPEAT_COUNT - 1))); do
                            nohup $PY_CMD $PYTHON_SCRIPT --index $i --config $DRONE_CONFIG_FILE --bandwidth $BANDWIDTH_FILE --message-size $m --cluster-size $cluster_size > /dev/null 2>&1 &
                            sleep 0.2
                        done
                        echo "============================================== Drone Started ============================================================>"
                        sleep 2
                        
                        # curl 요청 수행 (클라이언트 포트 20001)
                        echo "curl 요청 시작..."
                        CURL_RESPONSE=$(curl -s --max-time 600 -X POST http://localhost:20001/start-protocol \
                            -H "Content-Type: application/json" \
                            -d '{"latitude": 36.6261519, "longitude": 127.4590123, "altitude": 0}')
                        echo "===== CURL 응답 상세 내용 ====="
                        echo "$CURL_RESPONSE"
                        echo "==============================="
                        
                        echo "$CURL_RESPONSE" >> "$CONSENSUS_TIME_META_FILE"
                        echo "응답 데이터가 $CONSENSUS_TIME_META_FILE 에 저장되었습니다."
                        
                        # 유효한 JSON 데이터 추출
                        JSON_RESPONSE=$(echo "$CURL_RESPONSE" | grep -E '^\{.*\}$')
                        echo ">>>>>>>>>>>>>>>>>>> $JSON_RESPONSE"
                        
                        # JSON 응답에서 average_time 추출
                        avg_time=$(python -c "import sys, json; print(json.load(sys.stdin)['protocol']['average_time'])" <<< "$JSON_RESPONSE")
                        echo "추출된 average_time: $avg_time"
                        
                        # JSON 파일 업데이트
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
                        
                        echo "모든 작업이 완료되었습니다. kill_drone.sh 파일을 실행합니다."
                        ./kill_drone.sh "$DRONE_CONFIG_FILE"
                        sleep 1
                    done  # end ROUNDS loop
                done  # end DRONE loop
            done  # end COVERAGE_RADIUS loop
        done  # end CLUSTER_SIZE loop
    done  # end MESSAGE_SIZE loop
done  # end PROTOCOL loop