# 프로세스를 죽이는 쉘 스크립트 => 만약 권한이 없으면 chmod +x kill_pbft.sh 

# 포트를 사용하는 프로세스 ID를 찾아 종료하는 함수
kill_process_on_port() {
    PORT=$1 # $1는 함수 호출 시 전딜되는 첫 번째 인자 ex 함수 뒤 포트 번호
    PID=$(lsof -t -i:$PORT) # $() 는 괄호 안 명령어 출력값을 PID 변수에 할당함 -t : 출력을 PID로 한정 -i:&PORT 는 지정된 포트 번호로 필터링
    if [ -n "$PID" ]; then # -n : 문자열 변수가 비어 있는지 검사
        echo "Killing process on port $PORT with PID $PID"
        kill -9 $PID
    else
        echo "No process found on port $PORT"
    fi
}

# 종료할 포트 번호 목록 배열
PORTS=(40000 40001 40002 40003 20001)

# 각 포트에 대해 kill_process_on_port 함수 호출
for PORT in "${PORTS[@]}"; do # ${PORTS[@]} : PORTS 배열을 모두 순회하라
    kill_process_on_port $PORT
done
