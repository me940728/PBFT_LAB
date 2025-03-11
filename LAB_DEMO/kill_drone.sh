#!/bin/bash
# kill_drone_processes.sh
# 드론 설정 YAML 파일 (drone_info_control.yaml)에서 모든 드론의 포트를 추출하여,
# 해당 포트를 사용 중인 프로세스를 자동으로 종료하는 스크립트입니다.

# 드론 설정 파일
CONFIG_FILE="drone_info_control_200.yaml"

# CONFIG_FILE 존재 여부 확인
if [ ! -f "$CONFIG_FILE" ]; then
  echo "Error: $CONFIG_FILE not found!"
  exit 1
fi

# OS_TYPE 강제 지정 (윈도우)
OS_TYPE="win"
echo "OS => $OS_TYPE"

# YAML 파일에서 'drones' 항목의 모든 'port' 값을 추출 (공백으로 구분된 문자열)
PORTS=$(python3 -c "import yaml; d=yaml.safe_load(open('$CONFIG_FILE')); print(' '.join(str(drone['port']) for drone in d['drones']))")
echo "Found ports: $PORTS"

# 포트를 사용하는 프로세스를 종료하는 함수
kill_process_on_port() {
    PORT=$1
    if [ "$OS_TYPE" = "win" ]; then
        PID=$(netstat -ano | tr -d '\r' | grep ":$PORT" | awk '{print $NF}' | head -n 1)
        if [ -n "$PID" ]; then
            echo "Killing process on port $PORT with PID $PID"
            taskkill /PID "$PID" /F
        else
            echo "WIN-No process found on port $PORT"
        fi
    else
        # macOS/Linux: lsof와 kill 사용
        PID=$(lsof -t -i:$PORT)
        if [ -n "$PID" ]; then
            echo "Killing process on port $PORT with PID $PID"
            kill -9 $PID
        else
            echo "MAC-No process found on port $PORT"
        fi
    fi
}

# 추출된 각 포트에 대해 프로세스 종료
for PORT in $PORTS; do
    kill_process_on_port $PORT
done
