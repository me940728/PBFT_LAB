#!/usr/bin/env python3
"""
test_delay.py

이 스크립트는 PBFT 프로토콜에서 사용되는 메시지(예: 
{"status": "protocol completed", "total_time": 4.33895206451416, "round_times": [1.4314894676208496, 1.4622235298156738, 1.4452390670776367], "fault_nodes": []})
를 dump_message 함수를 사용해 1MB 크기로 패딩한 후, 
대역폭 데이터에 따른 전송 지연시간(simulate_delay)을 테스트하는 예시 코드입니다.
"""

import asyncio
import json
from common import dump_message, simulate_delay

async def main():
    # PBFT 프로토콜의 합의 완료 메시지 예시 (curl 명령어의 출력)
    message = {
        "status": "protocol completed",
        "total_time": 4.33895206451416,
        "round_times": [1.4314894676208496, 1.4622235298156738, 1.4452390670776367],
        "fault_nodes": []
    }
    
    # config에서 읽어올 m 값 (MB 단위). 여기서는 1MB로 설정합니다.
    m = 2
    
    # dump_message 함수를 사용하여 메시지를 m MB 크기로 패딩합니다.
    dumped_message = dump_message(message, m)
    
    # 패딩된 메시지를 JSON 문자열로 변환 후 크기를 계산합니다.
    dumped_json = json.dumps(dumped_message)
    message_size_bytes = len(dumped_json.encode('utf-8'))
    message_size_bits = message_size_bytes * 8
    print(f"Dumped message size: {message_size_bytes} bytes, {message_size_bits} bits")
    
    # 대역폭 데이터를 정의합니다.
    # 예: 거리 0~260m: 10 Mbps, 260m 이상: 5 Mbps
    bandwidth_data = {
        "bandwidth_by_distance": [
            {"range": "0-260", "bandwidth": 10},
            {"range": "260-", "bandwidth": 5}
        ]
    }
    
    # 테스트할 거리 (예: 260m)
    test_distance = 260
    
    # simulate_delay 함수를 호출하여 지정된 거리와 메시지 크기에 따른 전송 딜레이를 계산합니다.
    delay = await simulate_delay(test_distance, message_size_bits, bandwidth_data)
    print(f"Simulated delay for {test_distance} m and message size {message_size_bits} bits: {delay} sec")

if __name__ == "__main__":
    asyncio.run(main())
