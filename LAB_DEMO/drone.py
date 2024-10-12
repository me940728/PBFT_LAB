import sys
import yaml
import asyncio
import aiohttp
from aiohttp import web
from geopy.distance import geodesic
import os
import logging
from datetime import datetime
import math
import time

# 유클리드 거리 계산 함수 (전역 함수로 정의)
def calculate_euclidean_distance(coords1, coords2):
    """
    두 좌표 간의 유클리드 거리를 계산하는 함수.
    coords1: (위도, 경도, 고도)로 구성된 첫 번째 좌표
    coords2: (위도, 경도, 고도)로 구성된 두 번째 좌표
    """
    flat_distance = geodesic(coords1[:2], coords2[:2]).meters  # 평면 거리 계산
    altitude_difference = abs(coords1[2] - coords2[2])  # 고도 차이 계산
    euclidean_distance = math.sqrt(flat_distance**2 + altitude_difference**2)  # 유클리드 거리 계산
    return euclidean_distance

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
'''
class-1 위도, 경도, 고도 PBFT 객체
'''
class LLAPBFTHandler:
    LLAPBFT_START = 'llapbft-start'
    DISTANCE_REQ = 'distance-request'

    def __init__(self, index, node, nodes, session, bandwidth_file):
        self.index = index      # 고유 아이디(run시 부여)
        self.node = node        # 현재 드론의 정보
        self.nodes = nodes      # 전체 드론 리스트
        self.session = session  # aiohttp 세션 재사용
        self.f = 1              # 악의 노드 수 설정 (여기서 조정 가능)
        self.seq = 0            # 메시지 시퀀스 번호
        self.responses = {}     # 시퀀스 번호별 응답 저장
        # LatencySimulation 객체 생성 매번 호출하는 것이 비효율적임
        self.latency_simulation = LatencySimulation(bandwidth_file)

    def increment_seq(self, amount=1):
        """self.seq 변수를 증가시킴"""
        self.seq += amount
        return self.seq

    def reset_seq(self):
        """self.seq를 0으로 초기화"""
        self.seq = 0

    def get_bft(self):
        """3f + 1 계산을 기반으로 응답 임계값 반환"""
        return 3 * self.f + 1

    async def start_llapbft(self, request):
        try:
            data = await request.json()
            client_num = self.index
            client_latitude = data['latitude']
            client_longitude = data['longitude']
            client_altitude = data['altitude']

            logging.info(f"[Client]Received distance request from client at ({client_num}, {client_latitude}, {client_longitude}, {client_altitude})")

            # 클라이언트 -> 드론에게 위치 정보 요청
            message_seq = self.increment_seq()
            await self.request_distances_from_other_drones(client_num, message_seq, client_latitude, client_longitude, client_altitude)

            return web.json_response({'status': 'Distance requests sent and waiting for responses'})

        except Exception as e:
            logging.error(f"Error handling distance request: {str(e)}")
            return web.json_response({'error': str(e)}, status=500)

    async def handle_request(self, client_num, target_node, client_coords, message_seq):
        try:
            url = f"http://{target_node['host']}:{target_node['port']}/pre-request/{LLAPBFTHandler.DISTANCE_REQ}"
            async with self.session.post(url, json={
                'client_num': client_num,
                'latitude': client_coords[0],
                'longitude': client_coords[1],
                'altitude': client_coords[2],
                'seq': message_seq
            }) as response:
                if response.status == 200:
                    res_data = await response.json()
                    return res_data
                return None
        except Exception as e:
            logging.error(f"Error retrieving distance from {target_node['host']}:{target_node['port']} - {str(e)}")
            return None
    # 클라이언트 -> 드론들 메시지 멀티 캐스팅
    async def request_distances_from_other_drones(self, client_num, message_seq, client_latitude, client_longitude, client_altitude):
        tasks = []  # 비동기 요청들을 담을 리스트
        client_coords = (client_latitude, client_longitude, client_altitude)
        
        # 멀티캐스트 시작 시각 기록
        start_time = time.time()
        #logging.info(f"Multicast start time: {start_time:.10f}")

        # 클라이언트 제외 모든 드론에게 위치정보 요청 메시지 보냄
        for target_node in self.nodes:
            if target_node != self.node:
                tasks.append(self.handle_request(client_num, target_node, client_coords, message_seq))

        # 응답 수집 (모든 드론의 응답을 기다림)
        responses = await asyncio.gather(*tasks)

        # 응답 완료 시각 기록
        end_time = time.time()
        #logging.info(f"Responses received time: {end_time:.10f}")
        
        # 전체 소요 시간 계산
        total_time = end_time - start_time
        logging.info(f"Total time for multicast and responses: {total_time:.10f} seconds")

        # 응답받은 메시지로부터 유클리드 거리를 계산하고 로그를 남김
        self.group_by_distance(responses, client_coords)
        
    # 드론 요청 정보를 종합하여 그룹핑 후 리더 드론에게 합의 요청하는 함수
    def group_by_distance(self, responses, client_coords):
        """
        응답받은 드론들로부터 유클리드 거리를 계산하고, 지연 시간 및 정보를 로그에 기록
        """
        client_port = self.node['port']
        
        for i, res in enumerate(responses):
            if res:
                drone_coords = (res['latitude'], res['longitude'], res['altitude'])
                # 유클리드 거리 계산 (전역 함수를 사용)
                euclidean_distance = calculate_euclidean_distance(client_coords, drone_coords)

                drone_port = self.nodes[i]['port']
                altitude = res['altitude']

                # 메시지 크기 계산
                message = f"{res}"
                _, message_size_bits = self.latency_simulation.get_message_size(message)

                # 지연 시간 계산
                latency = self.latency_simulation.get_latency(euclidean_distance, message_size_bits)

                # 로그 기록 (정수부 4자리, 소수부 10자리 포맷팅)
                logging.info(
                    f"client:{client_port} -> drone:{drone_port} {euclidean_distance:14.10f}m, "
                    f"altitude: {altitude:14.10f}m, lat: {res['latitude']:14.10f}, lon: {res['longitude']:14.10f}, "
                    f"latency: {latency:.10f}s"
                )
    # 드론이 클라이언트 요청에 응답하는 함수
    async def respond_distance(self, request):
        try:
            data = await request.json()
            seq = data['seq']

            logging.info(f"Received {LLAPBFTHandler.DISTANCE_REQ} from client for sequence {seq}")

            # 자신의 위치 정보
            drone_coords = (self.node['latitude'], self.node['longitude'], self.node['altitude'])
            client_coords = (data['latitude'], data['longitude'], data['altitude'])

            # 유클리드 거리 계산
            euclidean_distance = calculate_euclidean_distance(client_coords, drone_coords)

            # 메시지 크기 계산
            message = f"{data}"
            _, message_size_bits = self.latency_simulation.get_message_size(message)

            # 지연 시간 계산
            latency = self.latency_simulation.get_latency(euclidean_distance, message_size_bits)

            # 지연 시간만큼 대기
            logging.info(f"Latency for response: {latency:.10f}s")
            await asyncio.sleep(latency)

            # 자신의 위치 정보와 시퀀스 번호를 반환
            response = {
                'seq': seq,
                'index': self.index,
                'latitude': self.node['latitude'],
                'longitude': self.node['longitude'],
                'altitude': self.node['altitude']
            }

            # 요청받은 드론의 로그 기록
            logging.info(f"Responding to client with {response}")
            return web.json_response(response)

        except Exception as e:
            logging.error(f"Error in responding to distance request: {str(e)}")
            return web.json_response({'error': str(e)}, status=500)
'''
class-1 end
'''
'''
class-2 멀티캐스트 지연 시간을 재현하기 위한 지연 객체
'''
class LatencySimulation:
    def __init__(self, yaml_file):
        # YAML 파일 로드 및 저장
        self.bandwidth_config = self.load_bandwidth_config(yaml_file)

    # 메시지 크기를 바이트와 비트로 반환하는 함수
    def get_message_size(self, message: str):
        message_bytes = message.encode('utf-8')
        byte_size = len(message_bytes)
        bit_size = byte_size * 8
        return byte_size, bit_size

    # YAML 파일에서 구간별 대역폭 정보를 읽어오는 함수
    @staticmethod
    def load_bandwidth_config(yaml_file):
        with open(yaml_file, 'r') as file:
            return yaml.safe_load(file)

    # 거리 구간에 따른 대역폭 반환 함수
    def get_bandwidth_by_distance(self, distance):
        for entry in self.bandwidth_config['bandwidth_by_distance']:
            range_str = entry['range']
            if '-' in range_str:
                lower, upper = range_str.split('-')
                lower = int(lower)
                if upper:
                    upper = int(upper)
                    if lower <= distance <= upper:
                        return entry['bandwidth']
                else:
                    if distance >= lower:
                        return entry['bandwidth']
        return 0

    # 거리와 메시지 크기에 따른 지연 시간 계산 함수
    def get_latency(self, distance, message_size_bits):
        bandwidth_mbps = self.get_bandwidth_by_distance(distance)
        if bandwidth_mbps == 0:
            return float('inf')
        bandwidth_bps = bandwidth_mbps * 1_000_000
        latency_seconds = message_size_bits / bandwidth_bps
        return latency_seconds * 2  # 송신과 수신을 고려해 지연 시간 2배 반환
'''
class-2 end
'''

# main 함수
async def main():
    index = int(sys.argv[1])
    yaml_file = sys.argv[2]
    config = load_config(yaml_file)
    bandwidth_file = sys.argv[3] # 대역폭 정보를 담은 YAML 파일 경로 추가

    if index < len(config['drones']):
        node = config['drones'][index]
    else:
        print(f"인덱스 {index}는 유효하지 않습니다.")
        return

    nodes = config['drones']
    setup_logging(index)

    async with aiohttp.ClientSession() as session:
        lla_pbft = LLAPBFTHandler(index, node, nodes, session, bandwidth_file)

        app = web.Application()
        app.add_routes([
            web.post('/pre-request/' + LLAPBFTHandler.LLAPBFT_START, lla_pbft.start_llapbft),  # LLAPBFT 프로토콜 시작
            web.post('/pre-request/' + LLAPBFTHandler.DISTANCE_REQ, lla_pbft.respond_distance) # 클라이언트 -> 드론 위치 정보 요청
        ])

        host = node['host']
        port = node['port']
        print(f"[run] =======> index : {index} / host : {host} / port : {port}")
        
        try:
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host=host, port=port)
            await site.start()

            while True:
                await asyncio.sleep(3600)

        except Exception as e:
            logging.error(f"Error running web app: {str(e)}")

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(main())
        else:
            loop.run_until_complete(main())
    except RuntimeError as e:
        logging.error(f"Runtime error: {str(e)}")