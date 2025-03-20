#!/usr/bin/env python3
"""
pbft.py

PBFT 합의 시뮬레이션 코드 (지연시간 반영 버전)
- YAML 구성 파일에는 protocol, ostype, f, k, drones 등이 포함됨
- drones 리스트 중 port가 20001인 항목은 클라이언트 역할, 나머지는 복제자(노드)로 동작함
- 로그는 log/pbft 폴더에 기록되며, 송신/수신/예외 로그와 각 라운드 소요시간 및 최종 평균이 출력됨
- 수신 로그 예:
  [2025-03-19 12:23:35,879] [INFO] [30017] Received COMMIT: id:1742354615195, from:30019, delay:00.0001s, distance:142.11m (Euclidean), mes_siz_bit:8388608
- 클라이언트는 /start-protocol POST 요청으로 합의 라운드를 시작함
"""

import asyncio
import aiohttp
from aiohttp import TCPConnector, web
import logging
import time
import argparse
import os
import yaml
import json
import sys
from common import calculate_distance, simulate_delay, dump_message
import gc

if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # Windows용 이벤트 루프 정책 적용됨

##########################################################################
# 송신 및 지연 적용 함수 (필요시 딜레이를 적용하여 메시지를 전송함)
##########################################################################
async def send_with_delay(session: aiohttp.ClientSession, sender: dict, receiver: dict, url: str, message: dict, bw_data: dict):
    if bw_data:
        sender_coords = (sender.get('latitude', 0), sender.get('longitude', 0), sender.get('altitude', 0))
        receiver_coords = (receiver.get('latitude', 0), receiver.get('longitude', 0), receiver.get('altitude', 0))
        dist = calculate_distance(sender_coords, receiver_coords)
        # 300m 이상이면 fault 처리됨
        if dist >= 300:
            logging.getLogger("DelayLogger").info(
                f"[FAULT] Sender: Port{sender['port']} → Receiver: Port{receiver['port']} | "
                f"Distance: {dist:.2f} m exceeds threshold; message marked as fault"
            )
            message["fault"] = True
            return "Fault: Message not delivered due to extreme distance"
        # 메시지 크기를 JSON 직렬화 후 비트 단위로 계산됨
        msg_size_bits = len(json.dumps(message)) * 8
        delay = await simulate_delay(dist, msg_size_bits, bw_data)
        # 딜레이, 거리, 메시지 크기 정보를 메시지에 추가함
        message["simulated_delay"] = delay
        message["distance"] = dist
        message["message_size_bits"] = msg_size_bits
    try:
        async with session.post(url, json=message) as resp:
            return await resp.text()
    except Exception as e:
        logging.getLogger("DelayLogger").exception(f"Error in send_with_delay: {e}")
        raise

##########################################################################
# 로깅 설정 함수 (지정된 경로에 로그를 기록함)
##########################################################################
def setup_logging(name: str, log_filename: str) -> logging.Logger:
    log_directory = os.path.join(os.getcwd(), "log", "pbft")
    os.makedirs(log_directory, exist_ok=True)
    log_path = os.path.join(log_directory, log_filename)
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fmt = logging.Formatter('[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s')
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)
        file_handler = logging.FileHandler(log_path, mode='a')
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    return logger

##########################################################################
# 대역폭 데이터 로드 함수 (YAML 파일에서 대역폭 정보를 읽어옴)
##########################################################################
def load_bw_data(filepath: str) -> dict:
    if filepath and os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return None

##########################################################################
# 합의 상태 클래스 (각 합의 요청의 상태를 관리함)
##########################################################################
class ConsensusStatus:
    PREPREPARED = 'pre-prepared'
    PREPARED = 'prepared'
    COMMITTED = 'committed'
    REPLIED = 'replied'
    def __init__(self, f: int):
        self.f = f
        self.phase = None
        self.preprepare_msg = None
        self.prepare_msgs = []
        self.commit_msgs = []
        self.reply_sent = False
        self.finished = False  # 합의 완료 여부 플래그['25.3.19 Mem 관리 조치]
    def add_prepare(self, msg: dict):
        self.prepare_msgs.append(msg)
    def add_commit(self, msg: dict):
        self.commit_msgs.append(msg)
    def is_prepared(self) -> bool:
        return len(self.prepare_msgs) >= (2 * self.f)
    def is_committed(self) -> bool:
        return len(self.commit_msgs) >= (2 * self.f + 1)

##########################################################################
# PBFT 클라이언트 클래스 (합의 요청 생성 및 응답 대기)
##########################################################################
class PBFTClient:
    def __init__(self, config: dict, logger: logging.Logger, bw_filepath: str = None):
        self.sleep_time = 2
        self.config = config
        self.logger = logger
        # 클라이언트 역할은 port 20001인 드론
        self.client_info = next(d for d in config['drones'] if d['port'] == 20001)
        self.replicas = [d for d in config['drones'] if d['port'] != 20001]
        self.f = config.get('f', 1)
        self.session = aiohttp.ClientSession(
            connector=TCPConnector(limit=0, force_close=False),
            timeout=aiohttp.ClientTimeout(total=9999)
        )
        self.bw_data = load_bw_data(bw_filepath)
        self.total_rounds = 1  # 합의 라운드 수
        self.reply_events = {}  # {request_id: {"event": asyncio.Event(), "count": int}}
        self.semaphore = asyncio.Semaphore(500)
        self.m = config.get('m', 1)  # 최종 메시지 크기 (MB 단위)

    async def cleanup_finished_statuses(self):
        finished_requests = [req_id for req_id, status in self.statuses.items() if status.finished]
        for req_id in finished_requests:
            del self.statuses[req_id]
        # 필요하면 가비지 컬렉션도 호출
        import gc
        gc.collect()

    def get_leader(self) -> dict:
        # 리더는 복제자 중 port가 가장 작은 노드
        return sorted(self.replicas, key=lambda d: d['port'])[0]

    async def send_request(self, req_id: int = None, padded_payload: dict = None):
        if req_id is None:
            req_id = int(time.time() * 1000)
        leader = self.get_leader()
        url = f"http://{leader['host']}:{leader['port']}/request"
        # padded_payload가 있으면 이를 사용함, 없으면 기본 메시지에 대해 dump_message 호출함
        if padded_payload is not None:
            message_content = json.dumps(padded_payload)
            request_data = {
                "request_id": req_id,
                "timestamp": time.time(),
                "data": message_content
            }
        else:
            request_data = {
                "request_id": req_id,
                "timestamp": time.time(),
                "data": f"message_{req_id}"
            }
            request_data = dump_message(request_data, self.m)
        self.reply_events[req_id] = {"event": asyncio.Event(), "count": 0}
        async with self.semaphore:
            await send_with_delay(self.session, self.client_info, leader, url, request_data, self.bw_data)
        try:
            await self.reply_events[req_id]["event"].wait()
        except asyncio.TimeoutError:
            self.logger.error(f"Timeout waiting for REPLY for request {req_id}")

    async def prewarm_connections(self):
        leader = self.get_leader()
        url = f"http://{leader['host']}:{leader['port']}/ping"
        async with self.semaphore:
            await send_with_delay(self.session, self.client_info, leader, url, {"ping": True}, self.bw_data)

    async def start_protocol(self, request: web.Request):
        payload = await request.json()
        self.logger.info(f"/start-protocol 입력값: {payload}")
        await self.prewarm_connections()
        padded_payload = dump_message(payload, self.m)
        dummy_req_id = int(time.time() * 1000)
        await self.send_request(dummy_req_id, padded_payload=padded_payload)
        total_start = time.time()
        round_durations = []
        for rnd in range(1, self.total_rounds + 1):
            round_start = time.time()
            req_id = int(time.time() * 1000)
            await self.send_request(req_id, padded_payload=padded_payload)
            await asyncio.sleep(1)
            duration = time.time() - round_start
            round_durations.append(duration)
            self.logger.info(f"Round {rnd}: {duration:.4f} seconds")
            # 라운드가 종료되면 자원 정리 및 잠시 휴식
            self.reply_events.clear()
            gc.collect()
            await asyncio.sleep(self.sleep_time)  # 라운드 사이 휴식
        total_sleep = self.total_rounds * self.sleep_time
        total_duration = (time.time() - total_start) - total_sleep
        avg_duration = sum(round_durations) / len(round_durations)
        self.logger.info(f"[][][][]===> CONSENSUS COMPLETED in {total_duration:.4f} seconds, Average Round: {avg_duration:.4f} seconds")
        fault_nodes = []
        leader_info = self.get_leader()
        leader_coords = (leader_info.get('latitude', 0), leader_info.get('longitude', 0), leader_info.get('altitude', 0))
        for replica in self.replicas:
            replica_coords = (replica.get('latitude', 0), replica.get('longitude', 0), replica.get('altitude', 0))
            if calculate_distance(leader_coords, replica_coords) >= 300:
                fault_nodes.append(replica['port'])
        return web.json_response({
            "status": "protocol completed",
            "total_time": total_duration,
            "round_times": round_durations,
            "fault_nodes": fault_nodes
        })

    async def handle_reply(self, request: web.Request):
        try:
            reply = await request.json()
        except Exception:
            self.logger.error("Error parsing JSON in handle_reply")
            return web.json_response({"status": "error parsing reply"}, status=400)
        req_id = reply.get("request_id")
        if req_id in self.reply_events:
            self.reply_events[req_id]["count"] += 1
            if self.reply_events[req_id]["count"] >= self.f + 1:
                self.reply_events[req_id]["event"].set()
        return web.json_response({"status": "reply received"})

    async def close(self):
        await self.session.close()

##########################################################################
# PBFT 노드 클래스 (복제자 역할; 수신 로그를 고정폭 형식으로 출력함)
##########################################################################
class PBFTNode:
    def __init__(self, index: int, config: dict, logger: logging.Logger, bw_filepath: str = None):
        self.index = index
        self.config = config
        self.logger = logger
        self.node_info = config['drones'][index]
        self.replicas = [d for d in config['drones'] if d['port'] != 20001]
        self.f = config.get('f', 1)
        self.leader = sorted(self.replicas, key=lambda d: d['port'])[0]
        self.is_leader = (self.node_info['port'] == self.leader['port'])
        self.statuses = {}
        self.session = aiohttp.ClientSession(
            connector=TCPConnector(limit=0, force_close=False),
            timeout=aiohttp.ClientTimeout(total=9999)
        )
        self.bw_data = load_bw_data(bw_filepath)
        self.semaphore = asyncio.Semaphore(500)

    async def cleanup_finished_statuses(self):
        finished_requests = [req_id for req_id, status in self.statuses.items() if status.finished]
        for req_id in finished_requests:
            del self.statuses[req_id]
        gc.collect()
        self.logger.info(f"Cleaned up {len(finished_requests)} finished statuses")

    def _log_received(self, msg_type: str, req_id: int, sender: int, delay: str, distance: float, msg_size_bits: int):
        # 수신 로그를 고정폭으로 출력함 (sender: 5자리, delay: 문자열 그대로, distance: 7자리(소수점 이하 2자리), 메시지 크기: 7자리)
        self.logger.info(
            f"Received {msg_type}: id:{req_id}, from:{sender:5d}, delay:{delay}, "
            f"distance:{distance:07.2f}m (Euclidean), mes_siz_bit:{msg_size_bits:07d}"
        )

    async def broadcast(self, endpoint: str, message: dict):
        tasks = []
        leader_coords = (self.leader.get('latitude', 0), self.leader.get('longitude', 0), self.leader.get('altitude', 0))
        for replica in self.replicas:
            if replica['port'] == self.node_info['port']:
                continue
            replica_coords = (replica.get('latitude', 0), replica.get('longitude', 0), replica.get('altitude', 0))
            if calculate_distance(leader_coords, replica_coords) >= 300:
                continue
            tasks.append(self.send_message(replica, endpoint, message))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def send_message(self, target: dict, endpoint: str, message: dict):
        url = f"http://{target['host']}:{target['port']}{endpoint}"
        async with self.semaphore:
            try:
                await send_with_delay(self.session, self.node_info, target, url, message, self.bw_data)
            except Exception as e:
                self.logger.error(f"Error sending to {url}: {e}")

    async def handle_request(self, request: web.Request):
        try:
            req_data = await request.json()
        except Exception:
            self.logger.error("Error parsing JSON in handle_request")
            return web.json_response({"status": "error parsing request"}, status=400)
        req_id = req_data.get("request_id")
        self.logger.info(f"Received REQUEST: id:{req_id}")
        preprepare_msg = {
            "request_id": req_id,
            "data": req_data.get("data"),
            "timestamp": time.time(),
            "leader": self.node_info['port']
        }
        self.statuses[req_id] = ConsensusStatus(self.f)
        self.statuses[req_id].phase = ConsensusStatus.PREPREPARED
        self.statuses[req_id].preprepare_msg = preprepare_msg
        await self.broadcast('/preprepare', preprepare_msg)
        return web.json_response({"status": "PRE-PREPARE broadcasted"})

    async def handle_preprepare(self, request: web.Request):
        try:
            preprep_data = await request.json()
        except Exception:
            self.logger.error("Error parsing JSON in handle_preprepare")
            return web.json_response({"status": "error parsing preprepare"}, status=400)
        req_id = preprep_data.get("request_id")
        sender = preprep_data.get("leader", 0)
        delay = preprep_data.get("simulated_delay", "0.0000s")
        distance = preprep_data.get("distance", 0.0)
        msg_size_bits = preprep_data.get("message_size_bits", 0)
        self._log_received("PRE-PREPARE", req_id, sender, delay, distance, msg_size_bits)
        if req_id not in self.statuses:
            self.statuses[req_id] = ConsensusStatus(self.f)
        self.statuses[req_id].preprepare_msg = preprep_data
        prepare_msg = {
            "request_id": req_id,
            "data": preprep_data.get("data"),
            "timestamp": time.time(),
            "sender": self.node_info['port']
        }
        await self.broadcast('/prepare', prepare_msg)
        return web.json_response({"status": "PREPARE broadcasted"})

    async def handle_prepare(self, request: web.Request):
        try:
            prep_data = await request.json()
        except Exception:
            self.logger.error("Error parsing JSON in handle_prepare")
            return web.json_response({"status": "error parsing prepare"}, status=400)
        req_id = prep_data.get("request_id")
        sender = prep_data.get("sender", 0)
        delay = prep_data.get("simulated_delay", "0.0000s")
        distance = prep_data.get("distance", 0.0)
        msg_size_bits = prep_data.get("message_size_bits", 0)
        self._log_received("PREPARE", req_id, sender, delay, distance, msg_size_bits)
        if req_id not in self.statuses:
            self.statuses[req_id] = ConsensusStatus(self.f)
        self.statuses[req_id].add_prepare(prep_data)
        if self.statuses[req_id].preprepare_msg is None:
            return web.json_response({"status": "PREPARE ignored due to missing PRE-PREPARE"})
        if self.statuses[req_id].is_prepared() and self.statuses[req_id].phase != ConsensusStatus.PREPARED:
            self.statuses[req_id].phase = ConsensusStatus.PREPARED
            commit_msg = {
                "request_id": req_id,
                "data": self.statuses[req_id].preprepare_msg.get("data"),
                "timestamp": time.time(),
                "sender": self.node_info['port']
            }
            self.statuses[req_id].add_commit(commit_msg)
            await self.broadcast('/commit', commit_msg)
            if self.is_leader and not self.statuses[req_id].reply_sent:
                self.statuses[req_id].reply_sent = True
                reply_msg = {
                    "request_id": req_id,
                    "data": self.statuses[req_id].preprepare_msg.get("data"),
                    "timestamp": time.time(),
                    "sender": self.node_info['port']
                }
                client_info = next(d for d in self.config['drones'] if d['port'] == 20001)
                await self.send_message(client_info, '/reply', reply_msg)
        return web.json_response({"status": "PREPARE processed"})

    async def handle_commit(self, request: web.Request):
        try:
            commit_data = await request.json()
        except Exception:
            self.logger.error("Error parsing JSON in handle_commit")
            return web.json_response({"status": "error parsing commit"}, status=400)
        req_id = commit_data.get("request_id")
        sender = commit_data.get("sender", 0)
        delay = commit_data.get("simulated_delay", "0.0000s")
        distance = commit_data.get("distance", 0.0)
        msg_size_bits = commit_data.get("message_size_bits", 0)
        self._log_received("COMMIT", req_id, sender, delay, distance, msg_size_bits)
        if req_id not in self.statuses:
            self.statuses[req_id] = ConsensusStatus(self.f)
        self.statuses[req_id].add_commit(commit_data)
        if self.statuses[req_id].is_committed() and not self.statuses[req_id].reply_sent:
            self.statuses[req_id].reply_sent = True
            preprepare_data = self.statuses[req_id].preprepare_msg
            reply_msg = {
                "request_id": req_id,
                "data": preprepare_data.get("data") if preprepare_data else None,
                "timestamp": time.time(),
                "sender": self.node_info['port']
            }
            client_info = next(d for d in self.config['drones'] if d['port'] == 20001)
            await self.send_message(client_info, '/reply', reply_msg)
            self.statuses[req_id].finished = True
            # 한 라운드가 끝난 후 cleanup 및 지연 처리
            await self.cleanup_finished_statuses()
            await asyncio.sleep(2)  # 2초 휴식 (필요에 따라 조정)
        return web.json_response({"status": "COMMIT processed"})


    async def handle_reply(self, request: web.Request):
        try:
            reply_data = await request.json()
        except Exception:
            self.logger.error("Error parsing JSON in handle_reply (node)")
            return web.json_response({"status": "error parsing reply"}, status=400)
        self.logger.info(f"REPLY received (log): {reply_data}")
        return web.json_response({"status": "REPLY received"})

    async def close(self):
        await self.session.close()

##########################################################################
# 설정 파일 로드 함수 (YAML 파일에서 설정을 읽어옴)
##########################################################################
def load_config(filepath: str) -> dict:
    with open(filepath, 'r') as f:
        return yaml.safe_load(f)

##########################################################################
# 웹 애플리케이션 실행 헬퍼 (애플리케이션을 실행하고 Runner를 반환함)
##########################################################################
async def run_app(app: web.Application, host: str, port: int, logger: logging.Logger) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    return runner

##########################################################################
# 메인 함수 (클라이언트와 노드 역할에 따라 애플리케이션을 실행함)
##########################################################################
async def main():
    parser = argparse.ArgumentParser(description="PBFT Simulation")
    parser.add_argument("--config", type=str, default="drone_info_control.yaml", help="Path to YAML configuration file")
    parser.add_argument("--index", type=int, required=True, help="Index in the drones list; client must have port 20001")
    parser.add_argument("--bandwidth", type=str, default="bandwidth_info.yaml", help="Path to bandwidth info YAML file")
    args = parser.parse_args()
    config = load_config(args.config)
    if config.get("protocol", "").lower() != "pbft":
        print("Error: Only pbft protocol is supported in this simulation.")
        return
    node_info = config["drones"][args.index]
    role = "client" if node_info["port"] == 20001 else "node"
    scenario_info = f"PBFT Scenario: f={config.get('f', 'N/A')}, k={config.get('k', 'N/A')}"
    if role == "client":
        logger = setup_logging("Client", "client.log")
        logger.info(f"{scenario_info} | Role: CLIENT")
        client = PBFTClient(config, logger, bw_filepath=args.bandwidth)
        # 클라이언트 웹 앱 생성 (client_max_size를 10MB로 설정하여 큰 메시지 허용됨)
        app = web.Application(client_max_size=10 * 1024 * 1024)
        app.add_routes([
            web.post('/reply', client.handle_reply),
            web.post('/start-protocol', client.start_protocol)
        ])
        client_addr = client.client_info
        runner = await run_app(app, client_addr['host'], client_addr['port'], logger)
        logger.info("Waiting for /start-protocol trigger (use curl to start consensus)...")
        while True:
            await asyncio.sleep(3600)
    else:
        logger = setup_logging(f"Node_{args.index:3}", f"node_{args.index}.log")
        logger.info(f"Starting node at {node_info['host']}:{node_info['port']} | index={args.index:3} | f={config.get('f')}, k={config.get('k')}")
        node_instance = PBFTNode(args.index, config, logger, bw_filepath=args.bandwidth)
        app = web.Application(client_max_size=10 * 1024 * 1024)
        app.add_routes([
            web.post('/request', node_instance.handle_request),
            web.post('/preprepare', node_instance.handle_preprepare),
            web.post('/prepare', node_instance.handle_prepare),
            web.post('/commit', node_instance.handle_commit),
            web.post('/reply', node_instance.handle_reply),
            web.get('/ping', lambda req: web.json_response({"status": "pong"}))
        ])
        node_addr = node_instance.node_info
        runner = await run_app(app, node_addr['host'], node_addr['port'], logger)
        while True:
            await asyncio.sleep(3600)
    try:
        pass
    except asyncio.CancelledError:
        logger.info("Shutdown signal received.")
    except Exception:
        logger.exception("Unexpected error in main loop:")
    finally:
        try:
            if role == "client":
                await client.close()
            else:
                await node_instance.close()
            await runner.cleanup()
        except Exception:
            logger.exception("Error during cleanup.")

if __name__ == "__main__":
    asyncio.run(main())