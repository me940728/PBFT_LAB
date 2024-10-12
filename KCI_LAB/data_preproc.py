import yaml

# 멀티캐스트 지연 시간을 재현하기 위한 지연 객체
class LatencySimulation:
    def __init__(self, yaml_file):
        # YAML 파일 로드
        self.bandwidth_config = self.load_bandwidth_config(yaml_file)

    # 메시지 크기를 바이트와 비트로 반환하는 함수
    def get_message_size(self, message: str):
        message_bytes = message.encode('utf-8')
        byte_size = len(message_bytes)  # 바이트 크기
        bit_size = byte_size * 8        # 비트 크기
        return byte_size, bit_size

    # YAML 파일에서 구간별 대역폭 정보를 읽어오는 함수
    @staticmethod #=> 클래스 인스턴스 만들지 않고도 접근 가능
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
                if upper:  # 상한이 있을 경우
                    upper = int(upper)
                    if lower <= distance <= upper:
                        return entry['bandwidth']
                else:  # 상한이 없을 경우
                    if distance >= lower:
                        return entry['bandwidth']
        return 0  # 대역폭이 없을 경우 0 반환

    # 거리와 메시지 크기에 따른 지연 시간 계산 함수
    def get_latency(self, distance, message_size_bits):
        bandwidth_mbps = self.get_bandwidth_by_distance(distance)  # 메가비트 단위
        if bandwidth_mbps == 0:
            return float('inf')  # 대역폭이 0이면 전송 불가로 무한대 시간 반환
        
        bandwidth_bps = bandwidth_mbps * 1_000_000  # 메가비트를 비트로 변환
        latency_seconds = message_size_bits / bandwidth_bps  # 전송 시간 계산 (초 단위)
        
        return latency_seconds * 2  # 송신과 수신을 고려해 지연 시간 2배 반환
        

# 테스트 코드
if __name__ == "__main__":
    # YAML 파일 경로
    yaml_file = '/Users/admin/Downloads/ll/envs/pbft/source/KCI_LAB/bandwidth_info.yaml'
    
    # LatencySimulation 객체 생성
    latency_sim = LatencySimulation(yaml_file)
    
    # 테스트할 메시지
    message = """{
        'client_num': '1',
        'latitude': 36.1234551,
        'longitude': 127.5555454,
        'altitude': 200,
        'seq': 1
    }"""

    # 메시지 크기 계산
    byte_size, bit_size = latency_sim.get_message_size(message)
    print(f"메시지 크기: {byte_size} 바이트, {bit_size} 비트")

    # 테스트 거리 및 메시지 크기
    test_distance = 52  # 52미터 예시
    latency = latency_sim.get_latency(test_distance, bit_size)
    print(f"거리 {test_distance}m에서 메시지 전송 지연 시간: {latency:.10f}초") # 보통 마이크로초로 끝날 거임