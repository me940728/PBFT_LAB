# common.py
import math
from geopy.distance import geodesic
import asyncio

def calculate_distance(coords1, coords2):
    """
    두 좌표 간의 거리(수평거리와 고도 차이를 고려)를 계산(유클리디안 거리 산출 방식)
    """
    flat_distance = geodesic(coords1[:2], coords2[:2]).meters
    altitude_diff = abs(coords1[2] - coords2[2])
    return math.sqrt(flat_distance**2 + altitude_diff**2)

def get_bandwidth(distance, bandwidth_data, use_long=False):
    """
    주어진 거리와 bandwidth_data에 따라 대역폭을 반환
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
    주어진 거리와 메시지 크기에 따른 전송 딜레이를 시뮬레이션
    """
    bw = get_bandwidth(distance, bandwidth_data)
    if bw <= 0:
        delay = 0
    else:
        delay = message_size_bits / (bw * 1_000_000)
    await asyncio.sleep(delay)
    return delay