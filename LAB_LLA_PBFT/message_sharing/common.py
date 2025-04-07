# common.py
import math
from geopy.distance import geodesic
import json, os

def calculate_distance(coords1, coords2):
    """
    두 좌표 간의 거리(수평거리와 고도 차이를 고려)를 계산 (유클리디안 거리)
    """
    flat_distance = geodesic(coords1[:2], coords2[:2]).meters
    altitude_diff = abs(coords1[2] - coords2[2])
    return math.sqrt(flat_distance**2 + altitude_diff**2)

def get_bandwidth(distance, bandwidth_data, use_long=False):
    """
    주어진 거리와 bandwidth_data에 따라 대역폭(Mbps)을 반환
    """
    key = 'bandwidth_by_long_distance' if use_long else 'bandwidth_by_distance'
    for entry in bandwidth_data.get(key, []):
        parts = entry['range'].split('-')
        low = int(parts[0])
        if parts[1] == '':
            if distance >= low:
                return entry['bandwidth']
        else:
            high = int(parts[1])
            if low <= distance <= high:
                return entry['bandwidth']
    return 0

async def simulate_delay(distance, message_size_bits, bandwidth_data):
    """
    주어진 거리와 메시지 크기에 따른 전송 딜레이를 초 단위 문자열("s" 포함)로 반환
    """
    bw = get_bandwidth(distance, bandwidth_data)
    if bw <= 0:
        delay = 0.0
    else:
        delay = message_size_bits / (bw * 1_000_000)
    return f"{delay:.4f}s"

def get_message_bit_size(padding_mb=1):
    """
    parameter_files 폴더 내 메시지 파일(message_<padding_mb>.json)의 크기를 bit 단위로 반환.
    padding_mb 값에 따라 message 파일명을 결정 (예: padding_mb=3 → message_3.json).
    파일이 없으면 0을 반환.
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    file_name = f"message_{padding_mb}.json"
    file_path = os.path.join(current_dir, "parameter_files", file_name)
    if os.path.exists(file_path):
        file_size_bytes = os.path.getsize(file_path)
        return file_size_bytes * 8  # bit 단위로 변환
    return 0

def dump_message_to_file():
    # 내부 로컬 변수 설정 (메시지 아이디 없이 데이터만 포함)
    message = {"data": "Test message"}
    m = 5  # 목표 파일 크기 (MB)
    base_file_name = "message_3"  # 예시: 기본 메시지 파일 이름
    extension = ".json"
    
    MB = 1024 * 1024
    target_size = m * MB

    json_str = json.dumps(message)
    current_size = len(json_str.encode('utf-8'))
    
    padding_size = target_size - current_size
    if padding_size < 0:
        padding_size = 0
    
    message["dump"] = "0" * padding_size
    final_json_str = json.dumps(message)
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    dir_path = os.path.join(current_dir, "parameter_files")
    os.makedirs(dir_path, exist_ok=True)
    
    file_name = base_file_name + extension
    file_path = os.path.join(dir_path, file_name)
    if os.path.exists(file_path):
        counter = 0
        while os.path.exists(os.path.join(dir_path, f"{base_file_name}_{counter}{extension}")):
            counter += 1
        file_name = f"{base_file_name}_{counter}{extension}"
        file_path = os.path.join(dir_path, file_name)
    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(final_json_str)
    
    return file_path
'''
if __name__ == "__main__":
    saved_file = dump_message_to_file()
    print(f"메시지가 저장된 파일 경로: {saved_file}")
    '''