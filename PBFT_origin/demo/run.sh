# '24.7.23 MAC M1 에서 실행 하였음 => ./run.sh
# [port 충돌 시]
# lsof -i :{portNum} => PID 확인
# kill {PID} => PID 죽이기
#================================
./run_node.sh # 노드를 먼저 실행
sleep 15      # 15초 대기
./run_client.sh # 클라이언트 실행
#================================
