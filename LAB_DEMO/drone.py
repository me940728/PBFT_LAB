import sys
import yaml
import asyncio
import aiohttp
from aiohttp import web
from geopy.distance import geodesic
import os
import logging
from datetime import datetime
'''
합의 Request 예시
curl -X POST http://localhost:20001/pre-request/distance \
-H "Content-Type: application/json" \
-d '{"latitude":36.6261519,"longitude":127.4590123, "altitude":100}'
'''
# YAML 파일에서 노드 정보를 읽어오기
def load_config(yaml_file):
    with open(yaml_file, 'r') as file:
        return yaml.safe_load(file)

# 로그 파일 저장 디렉터리 설정
LOG_DIR = 'log'
os.makedirs(LOG_DIR, exist_ok=True)  # 디렉토리 존재 여부를 확인하고 없으면 생성

# 로그 설정 함수
def setup_logging(drone_index):
    log_file = os.path.join(LOG_DIR, f'drone_{drone_index}_log.txt')
    logging.basicConfig(filename=log_file, level=logging.INFO, format='%(asctime)s %(message)s')

class LLAPBFTHandler:
    # 드론들에게 보내는 요청과 응답 상수
    PRE_REQUEST = 'distance-request'    # 거리 요청을 의미하는 상수
    DISTANCE_RES = 'distance-response'  # 거리 응답을 의미하는 상수

    def __init__(self, index, node, nodes, session):
        self.index = index  # 고유 아이디(run시 부여)
        self.node = node    # 현재 드론의 정보
        self.nodes = nodes  # 전체 드론 리스트
        self.session = session  # aiohttp 세션 재사용
        
    # 합의를 요청 시작 전 사전 요청(거리 계산 후 그룹)하는 함수
    async def get_distance(self, request):
        # 클라이언트가 다른 드론들에게 거리 요청을 보내는 역할
        try:
            data = await request.json()
            client_num = self.index
            client_latitude = data['latitude']
            client_longitude = data['longitude']
            client_altitude = data['altitude']

            logging.info(f"[Client]Received distance request from client at ({client_num}, {client_latitude}, {client_longitude}, {client_altitude})")

            # 자신을 제외한 다른 드론들에게 거리 요청
            distances = await self.request_distances_from_other_drones(client_num, client_latitude, client_longitude, client_altitude)

            # 응답 반환
            return web.json_response({'status': 'Distances processed and logged', 'distances': distances})

        except Exception as e:
            logging.error(f"Error handling distance request: {str(e)}")
            return web.json_response({'error': str(e)}, status=500)

    async def handle_request(self, client_num, target_node, client_coords):
        # 각 드론에게 위치 정보를 요청
        try:
            return {
                'client_num' : client_num,
                'latitude': target_node['latitude'],
                'longitude': target_node['longitude'],
                'altitude': target_node['altitude']
            }
        except Exception as e:
            logging.error(f"Error retrieving distance from {target_node['host']}:{target_node['port']} - {str(e)}")
            return None

    async def request_distances_from_other_drones(self, client_num, client_latitude, client_longitude, client_altitude):
        tasks = []  # 비동기 요청들을 담을 리스트
        client_coords = (client_latitude, client_longitude, client_altitude)  # 클라이언트의 위치 정보 포함 coordinates
        for target_node in self.nodes:
            if target_node != self.node:  # 자신을 제외한 다른 노드들에게 요청
                tasks.append(self.handle_request(client_num, target_node, client_coords))

        responses = await asyncio.gather(*tasks)
        distances = []
        for i, res in enumerate(responses):
            if res:
                # 거리 계산 후 로그에 기록
                drone_coords = (res['latitude'], res['longitude'], res['altitude'])
                distance = geodesic((client_coords[0], client_coords[1]), (drone_coords[0], drone_coords[1])).meters

                # 고도 차이 고려한 실제 거리 계산 (3차원 거리)
                altitude_difference = abs(client_coords[2] - drone_coords[2])
                distance_3d = (distance**2 + altitude_difference**2)**0.5  # 유클리드 거리 d : 평면 거리, h : 높이 3d = (d^2 + h^2의 루트)

                distances.append({'drone_port': self.nodes[i]['port'], 'distance': distance_3d, 'altitude': res['altitude']})

                # 응답받은 드론의 위치 정보를 그대로 로그에 기록
                client_port = self.node['port']
                drone_port = self.nodes[i]['port']
                logging.info(
                    f"client:{client_port} -> drone:{drone_port} {distance_3d:.2f}m, "
                    f"altitude: {res['altitude']}m, lat: {res['latitude']}, lon: {res['longitude']}"
                )
        return distances

# main 함수
async def main():
    # 인자 값으로 반복 횟수(인덱스)와 YAML 파일 경로 수신
    index = int(sys.argv[1])
    yaml_file = sys.argv[2]

    # YAML 파일 로드
    config = load_config(yaml_file)

    # 인덱스에 맞는 노드 정보 추출
    if index < len(config['drones']):
        node = config['drones'][index]
    else:
        print(f"인덱스 {index}는 유효하지 않습니다.")
        return

    # 모든 노드 정보 로드
    nodes = config['drones']

    # 로그 파일 설정
    setup_logging(index)

    # aiohttp 세션 생성
    async with aiohttp.ClientSession() as session:
        # PBFTHandler 객체 생성
        pbft = LLAPBFTHandler(index, node, nodes, session)

        # 웹 애플리케이션 생성
        app = web.Application()
        app.add_routes([
            web.post('/pre-request/' + LLAPBFTHandler.PRE_REQUEST, pbft.get_distance),  # 클라이언트 제외 모든 드론에게 위치 정보 요청
            web.post('/pre-request/' + LLAPBFTHandler.DISTANCE_RES, pbft.respond_distance),  # 클라이언트에게 자신의 위치를 응답
        ])

        # 웹 애플리케이션 실행
        host = node['host']
        port = node['port']
        print(f"[run] =======> index : {index} / host : {host} / port : {port}")
        
        try:
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host=host, port=port)
            await site.start()

            while True:
                await asyncio.sleep(3600)  # 서버를 계속 실행 상태로 유지

        except Exception as e:
            logging.error(f"Error running web app: {str(e)}")

# asyncio 이벤트 루프 실행
if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 이벤트 루프가 이미 실행 중이면 기존 루프에 태스크 추가
            loop.create_task(main())
        else:
            # 그렇지 않으면 새 이벤트 루프 실행
            loop.run_until_complete(main())
    except RuntimeError as e:
        logging.error(f"Runtime error: {str(e)}")