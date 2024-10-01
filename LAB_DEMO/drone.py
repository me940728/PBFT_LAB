import sys
import yaml
import asyncio
import aiohttp
from aiohttp import web
from geopy.distance import geodesic
import os
import logging
from datetime import datetime

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

# PBFTHandler 클래스
class PBFTHandler:
    PRE_REQUEST = 'distance'

    def __init__(self, node, nodes, session):
        self.node = node  # 현재 드론의 정보
        self.nodes = nodes  # 전체 드론 리스트
        self.session = session  # aiohttp 세션 재사용

    async def get_distance(self, request):
        # 클라이언트가 다른 드론들에게 거리 요청을 보내는 역할
        try:
            data = await request.json()
            client_latitude = data['latitude']
            client_longitude = data['longitude']

            logging.info(f"Received distance request from client at ({client_latitude}, {client_longitude})")

            # 자신을 제외한 다른 드론들에게 거리 요청
            distances = await self.request_distances_from_other_drones(client_latitude, client_longitude)

            # 응답 반환
            return web.json_response({'status': 'Distances processed and logged', 'distances': distances})

        except Exception as e:
            logging.error(f"Error handling distance request: {str(e)}")
            return web.json_response({'error': str(e)}, status=500)

    async def handle_request(self, target_node, client_coords):
        # 각 드론에게 위치 정보를 요청
        try:
            return {
                'latitude': target_node['latitude'],
                'longitude': target_node['longitude'],
                'altitude': target_node['altitude']
            }
        except Exception as e:
            logging.error(f"Error retrieving distance from {target_node['host']}:{target_node['port']} - {str(e)}")
            return None

    async def request_distances_from_other_drones(self, client_latitude, client_longitude):
        tasks = []
        client_coords = (client_latitude, client_longitude)
        for target_node in self.nodes:
            if target_node != self.node:  # 자신을 제외한 다른 노드들에게 요청
                tasks.append(self.handle_request(target_node, client_coords))

        responses = await asyncio.gather(*tasks)
        distances = []
        for i, res in enumerate(responses):
            if res:
                # 거리 계산 후 로그에 기록
                drone_coords = (res['latitude'], res['longitude'])
                distance = geodesic(client_coords, drone_coords).meters
                distances.append({'drone_port': self.nodes[i]['port'], 'distance': distance, 'altitude': res['altitude']})

                # 로그에 기록
                client_port = self.node['port']
                drone_port = self.nodes[i]['port']
                logging.info(f"client:{client_port} -> drone:{drone_port} {distance:.2f}m, altitude: {res['altitude']}m")

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
        pbft = PBFTHandler(node, nodes, session)

        # 웹 애플리케이션 생성
        app = web.Application()
        app.add_routes([
            web.post('/get/' + PBFTHandler.PRE_REQUEST, pbft.get_distance),  # /get/distance로 POST 요청 처리
        ])

        # 웹 애플리케이션 실행
        host = node['host']
        port = node['port']

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