#!/usr/bin/env python3
"""
drone_info_generator.py
파일 설명:
1. 클라이언트 정보를 첫번째 포트로 사용하고, 나머지 replica(Follwer) 드론은 랜덤 offset (최소 MIN_OFFSET_M 이상, 최대 제한 값)을 적용하여 생성함.
2. YAML 파일에는 "protocol", "f", "k", "drones" 항목만 포함되며,
   - drones 리스트의 첫번째 항목은 클라이언트 정보 (포트: 20001)
   - 이후 replica(Follower) 드론 정보에는 "offset_m" 속성이 포함되어 위도/경도/고도의 오프셋(미터 단위, 고도는 정수로 절삭) 값을 표시함.
3. 전체 드론 수는 TOTAL_DRONES (클라이언트를 포함)로 지정 만약 80이면 79개 드론이 생성되는 것임 1개는 클라이언트
4. 동일한 파일명이 존재하면 drone_info_control_0.yaml, _1.yaml, ... 과 같이 PostFIX 0 > 1 > 2 순차 증가
"""

import random
import math
import os
import yaml

# --- 설정 파라미터 ---
TOTAL_DRONES = 201     # 클라이언트를 포함한 전체 드론 수
K = 4                  # 군집 내 드론은 3f+1개 이상이어야 함
PROTOCOL = "pbft"   # 프로토콜: "llapbft" || "pbft" || "random"
F_VALUE = 1           # 허용되는 악의적 드론 수

CLIENT_INFO = {
    "host": "localhost",
    "port": 20001,
    "latitude": 36.6266,
    "longitude": 127.4585,
    "altitude": 0
}

REPLICA_PORT_START = 30000  # replica 드론의 포트 할당 시작 번호

'''
랜덤 offset 제한 (미터 단위)
172 x 172 x 172로 유클리드 거리를 산출하면 최악의 경우 299m까지 나옴
wifi 대역폭 최대 300m 이내에 들어오도록 설계할 수 있음
'''
MAX_LAT_OFFSET_M = 172    # 위도 최대 300m
MAX_LNG_OFFSET_M = 172    # 경도 최대 300m
MAX_ALTITUDE_M   = 172    # 고도 최대 300m

MIN_OFFSET_M = 1  # 오프셋의 최소값 (0보다 큰 값)

# --- 헬퍼 함수 ---
def meters_to_degrees_lat(meters):
    """미터를 위도 도 단위로 변환 (1도 위도 ≒ 111,000m)"""
    return meters / 111000.0

def meters_to_degrees_lng(meters, base_lat):
    """미터를 경도 도 단위로 변환 (1도 경도 ≒ 111,000m * cos(latitude))"""
    return meters / (111000.0 * math.cos(math.radians(base_lat)))

def generate_random_offset(max_m):
    """최소 MIN_OFFSET_M 이상 최대 max_m 이하의 랜덤 오프셋 생성 (미터 단위)"""
    return random.uniform(MIN_OFFSET_M, max_m)

def generate_replica_drone(client_info, replica_id):
    """
    클라이언트 정보를 기준으로 랜덤 오프셋을 적용해 replica 드론 정보를 생성합니다.
    replica_id는 0부터 시작하며, 드론의 포트는 REPLICA_PORT_START + replica_id로 지정됩니다.
    생성된 오프셋 값은 "offset_m" 속성으로 저장되며, 고도는 정수로 절삭됩니다.
    """
    base_lat = client_info["latitude"]
    base_lng = client_info["longitude"]
    
    lat_offset_m = generate_random_offset(MAX_LAT_OFFSET_M)
    lng_offset_m = generate_random_offset(MAX_LNG_OFFSET_M)
    alt_offset_m = generate_random_offset(MAX_ALTITUDE_M)
    
    lat_offset_deg = meters_to_degrees_lat(lat_offset_m)
    lng_offset_deg = meters_to_degrees_lng(lng_offset_m, base_lat)
    
    drone_info = {
        "host": "localhost",
        "port": REPLICA_PORT_START + replica_id,
        "latitude": round(base_lat + lat_offset_deg, 7),
        "longitude": round(base_lng + lng_offset_deg, 7),
        "altitude": int(alt_offset_m),
        "offset_m": f"lat: {int(lat_offset_m)} m, lng: {int(lng_offset_m)} m, alt: {int(alt_offset_m)} m"
    }
    return drone_info

def generate_drones(total_drones, client_info):
    """
    첫번째 드론은 클라이언트 정보로 사용하고, 나머지 드론들은 replica 드론으로 생성합니다.
    """
    drones = [client_info]  # 첫번째 드론: 클라이언트 정보
    num_replicas = total_drones - 1
    for i in range(num_replicas):
        drone = generate_replica_drone(client_info, i)
        drones.append(drone)
    return drones

def get_output_filename(base_name="drone_info_control.yaml"):
    """
    동일한 파일명이 존재하면 drone_info_control_0.yaml, _1.yaml, ... 형태로 파일명을 생성합니다.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_path = os.path.join(script_dir, base_name)
    if not os.path.exists(base_path):
        return base_path
    i = 0
    while True:
        new_name = f"drone_info_control_{i}.yaml"
        new_path = os.path.join(script_dir, new_name)
        if not os.path.exists(new_path):
            return new_path
        i += 1

def main():
    output_filename = get_output_filename()
    
    # drones 리스트 생성: 첫번째 항목은 클라이언트 정보, 이후 replica 드론들
    drones = generate_drones(TOTAL_DRONES, CLIENT_INFO)
    
    # YAML 데이터 구성 (clients 항목 없이 protocol, f, k, drones만 포함)
    data = {
        "protocol": PROTOCOL,
        "f": F_VALUE,
        "k": K,
        "drones": drones
    }
    
    with open(output_filename, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    
    print(f"YAML 파일이 생성되었습니다: {output_filename}")

if __name__ == "__main__":
    main()