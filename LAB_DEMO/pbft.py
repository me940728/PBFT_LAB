#!/usr/bin/env python3
"""
pbft.py

PBFT 합의 시뮬레이션 코드 (지연시간 반영 버전)
- YAML 구성 파일에는 protocol: pbft, ostype: win 또는 mac, f: 4, k: 6, drones: 리스트가 있습니다.
- drones 리스트 중 port가 20001인 항목은 클라이언트 역할을 담당합니다.
- 나머지는 복제자(노드)로 동작합니다.
- 로그는 현재 디렉터리의 log/pbft 폴더에 생성되며, 기존 로그 파일에 이어서 기록됩니다.
- 클라이언트는 기본적으로 /start-protocol 엔드포인트에서 POST 요청을 받으면 합의 라운드를 시작합니다.
  (예:
    MAC : curl -X POST http://localhost:20001/start-protocol -H "Content-Type: application/json" -d '{"latitude": 36.6261519, "longitude": 127.4590123, "altitude": 0}'
    WIN PowerShell : curl.exe -X POST http://localhost:20001/start-protocol -H "Content-Type: application/json" -d '{ "latitude": 36.6261519, "longitude": 127.4590123, "altitude": 0 }'
    WIN CMD : curl -X POST http://localhost:20001/start-protocol -H "Content-Type: application/json" -d "{\"latitude\": 36.6261519, \"longitude\": 127.4590123, \"altitude\": 0}"
  )
- 모든 합의 라운드가 종료되면 클라이언트는 ★★★ 와 같은 특수문자를 포함하여 전체 소요 시간을 로그에 남기고,
  fault 노드의 포트 리스트도 함께 출력합니다.
- 전송 시, 각 노드간 거리에 따른 지연시간(simulate_delay)이 적용되며, 그 결과(거리, delay 등)는 수신측 로그 맨 끝에 출력됩니다.
"""

import asyncio
import aiohttp
import logging
import time
import argparse
import os
import yaml
import json
import sys
from aiohttp import web
from common import calculate_distance, simulate_delay

if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

##########################################################################
# 헬퍼 함수: 지연시간 적용 후 메시지 전송
##########################################################################
async def send_with_delay(session: aiohttp.ClientSession, source: dict, target: dict, url: str, data: dict, bandwidth_data: dict):
    if bandwidth_data:
        source_coords = (source.get('latitude', 0), source.get('longitude', 0), source.get('altitude', 0))
        target_coords = (target.get('latitude', 0), target.get('longitude', 0), target.get('altitude', 0))
        distance = calculate_distance(source_coords, target_coords)
        if distance >= 300:
            delay_logger = logging.getLogger("DelayLogger")
            delay_logger.info(
                f"[FAULT] Sender: Port{source['port']} → Receiver: Port{target['port']} | "
                f"Distance: {distance:.2f} m exceeds threshold; message marked as fault"
            )
            data["fault"] = True
            return "Fault: Message not delivered due to extreme distance"
        message_size_bits = len(json.dumps(data)) * 8
        delay = await simulate_delay(distance, message_size_bits, bandwidth_data)
        delay_logger = logging.getLogger("DelayLogger")
        delay_logger.info(
            f"[DELAY] Sender: Port{source['port']} → Receiver: Port{target['port']} | "
            f"Distance: {distance:.2f} m | Message size: {message_size_bits} bit | Delay: {delay:.4f} sec"
        )
        data["simulated_delay"] = delay
        data["distance"] = distance
        data["message_size_bits"] = message_size_bits
    try:
        async with session.post(url, json=data) as resp:
            return await resp.text()
    except Exception as e:
        logging.getLogger("DelayLogger").exception(f"Error in send_with_delay: {e}")
        raise

##########################################################################
# 로깅 설정
##########################################################################
def setup_logging(name: str, log_file_name: str) -> logging.Logger:
    log_dir = os.path.join(os.getcwd(), "log", "pbft")
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, log_file_name)
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        logger.addHandler(ch)
        fh = logging.FileHandler(log_file_path, mode='a')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger

##########################################################################
# Bandwidth 데이터 로딩 헬퍼 함수
##########################################################################
def load_bandwidth_data(bandwidth_file: str) -> dict:
    if bandwidth_file and os.path.exists(bandwidth_file):
        with open(bandwidth_file, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return None

##########################################################################
# PBFT 합의 단계 상태 클래스
##########################################################################
class Status:
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
    def add_prepare(self, msg: dict):
        self.prepare_msgs.append(msg)
    def add_commit(self, msg: dict):
        self.commit_msgs.append(msg)
    def is_prepared(self) -> bool:
        return len(self.prepare_msgs) >= (2 * self.f)
    def is_committed(self) -> bool:
        return len(self.commit_msgs) >= (2 * self.f + 1)

##########################################################################
# PBFT Client 클래스 (클라이언트 역할: port 20001)
##########################################################################
class PBFTClient:
    def __init__(self, config: dict, logger: logging.Logger, bandwidth_file: str = None):
        self.config = config
        self.logger = logger
        self.client = next(d for d in config['drones'] if d['port'] == 20001)
        self.replicas = [d for d in config['drones'] if d['port'] != 20001]
        self.f = config.get('f', 1)
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=9999))
        self.bandwidth_data = load_bandwidth_data(bandwidth_file)
        self.total_rounds = 1
        # reply_events를 {요청ID: {"event": 이벤트 객체, "count": 응답 개수}} 형태로 사용
        self.reply_events = {}

    def get_leader(self) -> dict:
        sorted_replicas = sorted(self.replicas, key=lambda d: d['port'])
        return sorted_replicas[0]

    async def send_request(self):
        req_id = int(time.time() * 1000)
        leader = self.get_leader()
        url = f"http://{leader['host']}:{leader['port']}/request"
        data = {
            "request_id": req_id,
            "timestamp": time.time(),
            "data": f"message_{req_id}"
        }
        self.logger.info(f"[CLIENT] >> REQUEST {req_id} → Leader({leader['port']})")
        # ※ 변경: f+1개의 REPLY를 기다리기 위해 이벤트와 응답 카운터를 초기화합니다.
        self.reply_events[req_id] = {"event": asyncio.Event(), "count": 0}
        await send_with_delay(self.session, self.client, leader, url, data, self.bandwidth_data)
        try:
            # ※ 변경: f+1개의 REPLY가 수신될 때까지 대기합니다.
            await self.reply_events[req_id]["event"].wait()
        except asyncio.TimeoutError:
            self.logger.error(f"[CLIENT] Timeout waiting for REPLY for request {req_id}")
        self.logger.info(f"[CLIENT] << f+1 REPLY received for REQUEST {req_id}")

    async def start_protocol(self, request: web.Request):
        self.logger.info("[][][][][][]============> /start-protocol request received: Starting consensus round")
        total_start_time = time.time()
        round_times = []
        for i in range(1, self.total_rounds + 1):
            round_start = time.time()
            await self.send_request()
            await asyncio.sleep(1)
            round_duration = time.time() - round_start
            round_times.append(round_duration)
            self.logger.info(f"[CLIENT] Round {i} completed: duration = {round_duration:.4f} seconds")
        total_duration = time.time() - total_start_time
        self.logger.info(f"[][][][][][]============> CONSENSUS COMPLETED: Total Time = {total_duration:.4f} seconds")
        fault_nodes = []
        leader = self.get_leader()
        leader_coords = (leader.get('latitude', 0), leader.get('longitude', 0), leader.get('altitude', 0))
        for replica in self.replicas:
            replica_coords = (replica.get('latitude', 0), replica.get('longitude', 0), replica.get('altitude', 0))
            if calculate_distance(leader_coords, replica_coords) >= 300:
                fault_nodes.append(replica['port'])
        self.logger.info(f"Fault Nodes: {fault_nodes}")
        return web.json_response({
            "status": "protocol started",
            "total_time": total_duration,
            "round_times": round_times,
            "fault_nodes": fault_nodes
        })

    async def handle_reply(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("Error parsing JSON in handle_reply")
            return web.json_response({"status": "error parsing reply"}, status=400)
        req_id = data.get("request_id")
        self.logger.info(f"[CLIENT] REPLY: {data}")
        if req_id in self.reply_events:
            # ※ 변경: reply 수신 카운트를 증가시키고, f+1개가 도달하면 이벤트를 set
            self.reply_events[req_id]["count"] += 1
            if self.reply_events[req_id]["count"] >= self.f + 1:
                self.reply_events[req_id]["event"].set()
        return web.json_response({"status": "reply received"})

    async def close(self):
        try:
            await self.session.close()
        except Exception:
            logging.getLogger("Client").exception("Error closing client session.")

##########################################################################
# PBFT Node 클래스 (복제자 역할)
##########################################################################
class PBFTNode:
    def __init__(self, index: int, config: dict, logger: logging.Logger, bandwidth_file: str = None):
        self.index = index
        self.config = config
        self.logger = logger
        self.node_info = config['drones'][index]
        self.replicas = [d for d in config['drones'] if d['port'] != 20001]
        self.f = config.get('f', 1)
        self.k = config.get('k', None)
        self.leader = self.get_leader()
        self.is_leader = (self.node_info['port'] == self.leader['port'])
        self.statuses = {}
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=9999))
        self.bandwidth_data = load_bandwidth_data(bandwidth_file)

    def get_leader(self) -> dict:
        sorted_replicas = sorted(self.replicas, key=lambda d: d['port'])
        return sorted_replicas[0]

    # broadcast 함수: 모든 엔드포인트에 대해, 리더와의 거리가 300m 이상인 노드에게는 메시지를 보내지 않음
    async def broadcast(self, endpoint: str, message: dict):
        tasks = []
        leader_coords = (self.leader.get('latitude', 0), self.leader.get('longitude', 0), self.leader.get('altitude', 0))
        for replica in self.replicas:
            if replica['port'] == self.node_info['port']:
                continue
            replica_coords = (replica.get('latitude', 0), replica.get('longitude', 0), replica.get('altitude', 0))
            if calculate_distance(leader_coords, replica_coords) >= 300:
                self.logger.info(f"[{self.node_info['port']}] Not broadcasting to fault node: {replica['port']}")
                continue
            tasks.append(self.send_message(replica, endpoint, message))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def send_message(self, target: dict, endpoint: str, data: dict):
        url = f"http://{target['host']}:{target['port']}{endpoint}"
        try:
            await send_with_delay(self.session, self.node_info, target, url, data, self.bandwidth_data)
        except Exception as e:
            self.logger.exception(f"[{self.node_info['port']}] Error sending to {url}: {e}")

    # 모든 노드가 자체적으로 요청 처리하여 합의 프로토콜에 참여 (리다이렉트 제거)
    async def handle_request(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("Error parsing JSON in handle_request")
            return web.json_response({"status": "error parsing request"}, status=400)
        req_id = data.get("request_id")
        self.logger.info(f"[{self.node_info['port']}] Received REQUEST {req_id}")
        preprepare_msg = {
            "request_id": req_id,
            "data": data.get("data"),
            "timestamp": time.time(),
            "leader": self.node_info['port']
        }
        self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].phase = Status.PREPREPARED
        self.statuses[req_id].preprepare_msg = preprepare_msg
        self.logger.info(f"[{self.node_info['port']}] Broadcasting PRE-PREPARE for REQUEST {req_id}")
        await self.broadcast('/preprepare', preprepare_msg)
        return web.json_response({"status": "PRE-PREPARE broadcasted"})

    async def handle_preprepare(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("Error parsing JSON in handle_preprepare")
            return web.json_response({"status": "error parsing preprepare"}, status=400)
        req_id = data.get("request_id")
        if "simulated_delay" in data and "distance" in data:
            self.logger.info(
                f"[{self.node_info['port']}] Received PRE-PREPARE: id:{req_id}, from:Leader({data.get('leader')}), "
                f"delay:{data['simulated_delay']:.4f}s, distance:{data['distance']:.2f}m (Euclidean)"
            )
        else:
            self.logger.info(f"[{self.node_info['port']}] Received PRE-PREPARE: id:{req_id}, from:Leader({data.get('leader')})")
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].preprepare_msg = data
        prepare_msg = {
            "request_id": req_id,
            "data": data.get("data"),
            "timestamp": time.time(),
            "sender": self.node_info['port']
        }
        self.logger.info(f"[{self.node_info['port']}] Broadcasting PREPARE for REQUEST {req_id}")
        await self.broadcast('/prepare', prepare_msg)
        return web.json_response({"status": "PREPARE broadcasted"})

    async def handle_prepare(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("Error parsing JSON in handle_prepare")
            return web.json_response({"status": "error parsing prepare"}, status=400)
        req_id = data.get("request_id")
        sender = data.get("sender")
        if "simulated_delay" in data and "distance" in data:
            self.logger.info(
                f"[{self.node_info['port']}] Received PREPARE: id:{req_id}, from:{sender}, "
                f"delay:{data['simulated_delay']:.4f}s, distance:{data['distance']:.2f}m (Euclidean)"
            )
        else:
            self.logger.info(f"[{self.node_info['port']}] Received PREPARE: id:{req_id}, from:{sender}")
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].add_prepare(data)
        if self.statuses[req_id].preprepare_msg is None:
            self.logger.info(f"[{self.node_info['port']}] Missing PRE-PREPARE for REQUEST {req_id}; ignoring PREPARE")
            return web.json_response({"status": "PREPARE ignored due to missing PRE-PREPARE"})
        if self.statuses[req_id].is_prepared() and self.statuses[req_id].phase != Status.PREPARED:
            self.statuses[req_id].phase = Status.PREPARED
            commit_msg = {
                "request_id": req_id,
                "data": self.statuses[req_id].preprepare_msg.get("data"),
                "timestamp": time.time(),
                "sender": self.node_info['port']
            }
            self.statuses[req_id].add_commit(commit_msg)
            self.logger.info(f"[{self.node_info['port']}] Broadcasting COMMIT for REQUEST {req_id}")
            await self.broadcast('/commit', commit_msg)
            if self.is_leader and not self.statuses[req_id].reply_sent:
                self.statuses[req_id].reply_sent = True
                reply_msg = {
                    "request_id": req_id,
                    "data": self.statuses[req_id].preprepare_msg.get("data"),
                    "timestamp": time.time(),
                    "sender": self.node_info['port']
                }
                client = next(d for d in self.config['drones'] if d['port'] == 20001)
                self.logger.info(f"[LEADER {self.node_info['port']}] Sending REPLY for REQUEST {req_id} to CLIENT")
                await self.send_message(client, '/reply', reply_msg)
        return web.json_response({"status": "PREPARE processed"})

    async def handle_commit(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("Error parsing JSON in handle_commit")
            return web.json_response({"status": "error parsing commit"}, status=400)
        req_id = data.get("request_id")
        sender = data.get("sender")
        if "simulated_delay" in data and "distance" in data:
            self.logger.info(
                f"[{self.node_info['port']}] Received COMMIT: id:{req_id}, from:{sender}, "
                f"delay:{data['simulated_delay']:.4f}s, distance:{data['distance']:.2f}m (Euclidean)"
            )
        else:
            self.logger.info(f"[{self.node_info['port']}] Received COMMIT: id:{req_id}, from:{sender}")
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].add_commit(data)
        if self.statuses[req_id].is_committed() and not self.statuses[req_id].reply_sent:
            self.statuses[req_id].reply_sent = True
            reply_msg = {
                "request_id": req_id,
                "data": self.statuses[req_id].preprepare_msg.get("data"),
                "timestamp": time.time(),
                "sender": self.node_info['port']
            }
            client = next(d for d in self.config['drones'] if d['port'] == 20001)
            self.logger.info(f"[LEADER {self.node_info['port']}] Sending REPLY for REQUEST {req_id} to CLIENT")
            await self.send_message(client, '/reply', reply_msg)
        return web.json_response({"status": "COMMIT processed"})

    async def handle_reply(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("Error parsing JSON in handle_reply (node)")
            return web.json_response({"status": "error parsing reply"}, status=400)
        self.logger.info(f"[{self.node_info['port']}] Received REPLY (for logging): {data}")
        return web.json_response({"status": "REPLY received"})

    async def close(self):
        try:
            await self.session.close()
        except Exception:
            self.logger.exception("Error closing node session.")

##########################################################################
# 설정 파일 파싱 함수
##########################################################################
def load_config(path: str) -> dict:
    with open(path, 'r') as f:
        return yaml.safe_load(f)

##########################################################################
# 웹 서버 실행 헬퍼: 앱을 실행하고 runner를 반환 (cleanup 용)
##########################################################################
async def serve_app(app: web.Application, host: str, port: int, logger: logging.Logger) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    return runner

##########################################################################
# 메인 함수: 역할(클라이언트 vs. 노드)에 따라 웹 서버를 실행
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
    node = config["drones"][args.index]
    role = "client" if node["port"] == 20001 else "node"
    scenario_info = f"PBFT Scenario: f={config.get('f', 'N/A')}, k={config.get('k', 'N/A')}"
    if role == "client":
        logger = setup_logging("Client", "client.log")
        logger.info(f"{scenario_info} | Role: CLIENT")
    else:
        replicas = [d for d in config["drones"] if d["port"] != 20001]
        leader_port = sorted(replicas, key=lambda d: d["port"])[0]["port"]
        role_str = "Leader" if node["port"] == leader_port else "Replica"
        logger = setup_logging(f"Node_{args.index}", f"node_{args.index}.log")
        logger.info(f"Starting node at {node['host']}:{node['port']} | index={args.index:3} | f={config.get('f')}, k={config.get('k')} | Role: {role_str}")
    try:
        if role == "client":
            client = PBFTClient(config, logger, bandwidth_file=args.bandwidth)
            app = web.Application()
            app.add_routes([
                web.post('/reply', client.handle_reply),
                web.post('/start-protocol', client.start_protocol)
            ])
            client_addr = client.client
            runner = await serve_app(app, client_addr['host'], client_addr['port'], logger)
            logger.info("Waiting for /start-protocol trigger (use curl to start consensus)...")
            while True:
                await asyncio.sleep(3600)
        else:
            node_instance = PBFTNode(args.index, config, logger, bandwidth_file=args.bandwidth)
            app = web.Application()
            app.add_routes([
                web.post('/request', node_instance.handle_request),
                web.post('/preprepare', node_instance.handle_preprepare),
                web.post('/prepare', node_instance.handle_prepare),
                web.post('/commit', node_instance.handle_commit),
                web.post('/reply', node_instance.handle_reply),
            ])
            node_addr = node_instance.node_info
            runner = await serve_app(app, node_addr['host'], node_addr['port'], logger)
            while True:
                await asyncio.sleep(3600)
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
