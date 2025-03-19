# common.py
import math
from geopy.distance import geodesic
import asyncio

def calculate_distance(coords1, coords2):
    """
    두 좌표 간의 거리(수평거리와 고도 차이를 고려)를 계산 (유클리디안 거리 산출 방식)
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
    주어진 거리와 메시지 크기에 따른 전송 딜레이를 초 단위 문자열("s" 포함)로 반환합니다.
    """
    bw = get_bandwidth(distance, bandwidth_data)
    if bw <= 0:
        delay = 0.0
    else:
        delay = message_size_bits / (bw * 1_000_000)
    # 초 단위의 딜레이를 "s" 단위를 포함한 문자열로 반환 (예: "1.2345s")
    return f"{delay:.4f}s"

def dump_message(message: dict, m: int) -> dict:
    """
    메시지(dict)를 JSON 직렬화한 후, 그 크기를 바이트 단위로 측정하여,
    최종 메시지 크기가 무조건 m MB가 되도록 'dump' 필드에 패딩 문자열("0")을 추가
    
    매개변수:
      message: 원래의 메시지(dict)
      m: 최종 메시지 크기 (MB 단위, 예: 3 -> 3MB)
    
    반환:
      패딩된 메시지(dict)
      
    예:
      원본 메시지의 직렬화 결과가 162바이트라면,
      m=1이면 최종 메시지 크기가 1MB(1,048,576 바이트)가 되도록 (1,048,576 - 162)개의 "0"이 추가됩니다.
    """
    import json
    MB = 1024 * 1024  # 1MB = 1,048,576 바이트
    json_str = json.dumps(message)
    current_size = len(json_str.encode('utf-8'))
    #print("Original message size: {} bytes".format(current_size), flush=True)
    target_size = m * MB
    padding_size = target_size - current_size
    if padding_size < 0:
        padding_size = 0  # 이미 m MB 이상이면 패딩하지 않음
    message["dump"] = "0" * padding_size
    return message
