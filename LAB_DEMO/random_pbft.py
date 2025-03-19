#!/usr/bin/env python3
"""
LLAPBFT 합의 시뮬레이션 코드 (랜덤 클러스터링 적용)
- 클라이언트는 드론 위치(클라이언트를 제외한)를 무작위로 k개의 클러스터로 분할하고,
  각 클러스터에서 클라이언트와의 거리는 고려하지 않고, 클러스터 내 드론들 중 임의로 매니저 드론을 선출하며,
  매니저 드론을 제외한 클러스터 구성원 중 임의로 리더 드론을 선출합니다.
- 클라이언트는 클러스터 매니저에게 PRE-REQUEST 메시지를 전송합니다.
- 클러스터 매니저는 클라이언트로부터 PRE-REQUEST 메시지를 수신한 후, 이를 클러스터 리더에게 REQUEST 메시지로 전달합니다.
- 클러스터 리더는 클러스터 내(매니저 제외) 팔로워들에게 PRE-PREPARE 메시지를 브로드캐스트하고,
  이후 PREPARE 및 COMMIT 단계를 진행한 후 클러스터 매니저에게 PRE-REPLY 메시지를 전송합니다.
- 클러스터 매니저는 클러스터 노드(리더 포함)로부터 f+1개의 PRE-REPLY 메시지를 수신하면 최종 REPLY를 클라이언트에게 전송합니다.
- 클러스터 매니저와 클라이언트 사이의 거리가 300m 이상이거나 클러스터 크기가 3f+1 미만이면 해당 클러스터는 합의에서 제외됩니다.
- 전송 시 simulate_delay가 적용되며, 클러스터링 정보는 별도의 로그 파일(clustering.log)에 기록됩니다.
- 워밍업 라운드를 포함하여 내부 캐시 및 리소스를 초기화함으로써 정확한 성능 측정을 목표로 합니다.
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
import random  # 랜덤 클러스터링을 위해 추가
from common import calculate_distance, simulate_delay
import numpy as np

if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

##########################################################################
# 유틸리티 함수: 시뮬레이션 지연을 적용하여 메시지 전송
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
# 유틸리티 함수: 로깅 설정
##########################################################################
def setup_logging(logger_name: str, log_filename: str) -> logging.Logger:
    log_dir = os.path.join(os.getcwd(), "log", "randompbft")
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, log_filename)
    logger = logging.getLogger(logger_name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
        file_handler = logging.FileHandler(log_file_path, mode='a')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    return logger

##########################################################################
# 유틸리티 함수: 대역폭 데이터 로드
##########################################################################
def load_bandwidth_data(bandwidth_filepath: str) -> dict:
    if bandwidth_filepath and os.path.exists(bandwidth_filepath):
        with open(bandwidth_filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return None

##########################################################################
# 합의 상태 클래스 (PBFT 유사)
##########################################################################
class Status:
    PREPREPARED = 'pre-prepared'
    PREPARED = 'prepared'
    COMMITTED = 'committed'
    PREREPLIED = 'pre-replied'
    REPLIED = 'replied'
    def __init__(self, f: int):
        self.f = f
        self.phase = None
        self.preprepare_msg = None
        self.prepare_msgs = []
        self.commit_msgs = []
        self.prereply_msgs = []
        self.reply_sent = False
    def add_prepare(self, msg: dict):
        self.prepare_msgs.append(msg)
    def add_commit(self, msg: dict):
        self.commit_msgs.append(msg)
    def add_prereply(self, msg: dict):
        self.prereply_msgs.append(msg)
    def is_prepared(self) -> bool:
        return len(self.prepare_msgs) >= (2 * self.f)
    def is_committed(self) -> bool:
        return len(self.commit_msgs) >= (2 * self.f + 1)
    def is_prereplied(self) -> bool:
        return len(self.prereply_msgs) >= (self.f + 1)

##########################################################################
# LLAPBFT 클라이언트 클래스 (랜덤 클러스터링 방식)
##########################################################################
class LLAPBFTClient:
    def __init__(self, config: dict, logger: logging.Logger, bandwidth_file: str = None):
        self.config = config
        self.logger = logger
        # 클라이언트는 포트 20001인 드론으로 지정
        self.client = next(d for d in config['drones'] if d['port'] == 20001)
        # 클라이언트를 제외한 모든 드론
        self.drones = [d for d in config['drones'] if d['port'] != 20001]
        self.f = config.get('f', 1)
        self.k = config.get('k', 1)
        self.session = aiohttp.ClientSession(
            connector=TCPConnector(limit=0, force_close=False),
            timeout=aiohttp.ClientTimeout(total=9999)
        )
        self.bandwidth_data = load_bandwidth_data(bandwidth_file)
        self.reply_events = {}
        self.reply_count = 0
        self.reply_cluster_ids = []
        self.reply_condition = asyncio.Condition()
        # 클러스터링 로그 설정
        self.cluster_logger = setup_logging("Clustering", "clustering.log")
        # 랜덤 클러스터링 수행: 클러스터 및 매니저, 리더 선출
        self.clusters = self.perform_clustering()
        # 실제 측정 라운드 수
        self.total_rounds = 100

    # 랜덤 클러스터링을 수행하여 유효한 클러스터 정보를 생성
    def perform_clustering(self):
        # 각 드론에 대해 0부터 k-1 사이의 무작위 클러스터 ID 할당
        drones_copy = self.drones.copy()
        for d in drones_copy:
            d['cluster_id'] = random.randint(0, self.k - 1)
        # 클러스터 ID별로 드론 그룹화
        clusters_dict = {}
        for d in drones_copy:
            cid = d['cluster_id']
            clusters_dict.setdefault(cid, []).append(d)
        clusters = []
        for cluster_id, group in clusters_dict.items():
            if not group:
                continue
            # 클러스터 매니저: 그룹 내에서 무작위로 선정
            cluster_manager = random.choice(group)
            # 클러스터 리더: 매니저 제외한 그룹 내 드론 중 무작위 선정 (없으면 매니저로 대체)
            remaining = [drone for drone in group if drone != cluster_manager]
            if remaining:
                cluster_leader = random.choice(remaining)
            else:
                cluster_leader = cluster_manager
            cluster_ports = [drone['port'] for drone in group]
            self.cluster_logger.info(f"Random Cluster {cluster_id}: Manager: {cluster_manager['port']}, Leader: {cluster_leader['port']}, Members: {cluster_ports}")
            clusters.append({
                "cluster_id": cluster_id,
                "cluster_manager": cluster_manager,
                "cluster_leader": cluster_leader,
                "cluster_ports": cluster_ports,
                "center": None  # 랜덤 방식에서는 클러스터 센터 계산 생략
            })
        return clusters

    # 각 클러스터 매니저에게 PRE-REQUEST 메시지 전송
    async def send_request_to_cluster(self, cluster, request_id):
        if len(cluster["cluster_ports"]) < (3 * self.f + 1):
            self.logger.info(f"[CLIENT] Excluding Cluster {cluster['cluster_id']}: insufficient drones (required: {3*self.f+1}, current: {len(cluster['cluster_ports'])})")
            return
        client_coords = (self.client.get('latitude', 0), self.client.get('longitude', 0), self.client.get('altitude', 0))
        manager = cluster["cluster_manager"]
        manager_coords = (manager.get('latitude', 0), manager.get('longitude', 0), manager.get('altitude', 0))
        distance = calculate_distance(client_coords, manager_coords)
        if distance >= 300:
            self.logger.info(f"[CLIENT] Excluding Cluster {cluster['cluster_id']}: distance between client and manager ({manager['port']}) is {distance:.2f}m (>= 300m)")
            return
        data = {
            "request_id": request_id,
            "timestamp": time.time(),
            "data": f"llapbft_message_{request_id}",
            "cluster_id": cluster["cluster_id"],
            "cluster_ports": cluster["cluster_ports"],
            "leader": cluster["cluster_leader"]['port'],
            "client_port": self.client['port'],
            "origin": "client"
        }
        url = f"http://{manager['host']}:{manager['port']}/request"
        self.logger.info(f"[CLIENT] Sending PRE-REQUEST to Cluster Manager {manager['port']} for Cluster {cluster['cluster_id']}")
        self.reply_events[cluster["cluster_id"]] = asyncio.Event()
        await send_with_delay(self.session, self.client, manager, url, data, self.bandwidth_data)

    # 사전 연결: 각 클러스터 매니저와 미리 연결
    async def prewarm_connections(self):
        managers = [cluster["cluster_manager"] for cluster in self.clusters]
        tasks = []
        for manager in managers:
            url = f"http://{manager['host']}:{manager['port']}/ping"
            tasks.append(send_with_delay(self.session, self.client, manager, url, {"ping": True}, self.bandwidth_data))
        await asyncio.gather(*tasks, return_exceptions=True)
        self.logger.info("Prewarm connections completed.")

    # 합의 프로토콜 시작 (워밍업 라운드 포함)
    async def start_protocol(self, request: web.Request):
        self.logger.info("[][][][][][]============> /start-protocol request received: Starting consensus rounds")
        # 사전 연결 실행
        await self.prewarm_connections()
        # 워밍업 라운드 실행 (측정에 포함하지 않음)
        self.logger.info("Starting warm-up round to stabilize connections...")
        dummy_request_id = int(time.time() * 1000)
        dummy_tasks = [self.send_request_to_cluster(cluster, dummy_request_id) for cluster in self.clusters]
        await asyncio.gather(*dummy_tasks, return_exceptions=True)
        async with self.reply_condition:
            await self.reply_condition.wait_for(lambda: self.reply_count >= (self.f + 1))
        self.reply_count = 0  # 워밍업 후 카운트 초기화
        self.logger.info("Warm-up round completed. Starting measured rounds...")
        # 타이밍 측정 재설정
        total_start_time = time.time()
        round_times = []
        for round_num in range(1, self.total_rounds + 1):
            round_start = time.time()
            request_id = int(time.time() * 1000)
            tasks = [self.send_request_to_cluster(cluster, request_id) for cluster in self.clusters]
            await asyncio.gather(*tasks, return_exceptions=True)
            self.logger.info("All PRE-REQUEST messages sent → Waiting for REPLY from cluster managers")
            async with self.reply_condition:
                await self.reply_condition.wait_for(lambda: self.reply_count >= (self.f + 1))
            round_duration = time.time() - round_start
            round_times.append(round_duration)
            self.logger.info(f"[CLIENT] Round {round_num} completed: duration = {round_duration:.4f} seconds")
            self.reply_count = 0
        total_duration = time.time() - total_start_time
        avg_round_time = sum(round_times) / len(round_times)
        self.logger.info(f"[][][][] LLAPBFT Consensus completed: Total time = {total_duration:.4f} seconds, "
                         f"Average Round Time = {avg_round_time:.4f} seconds")
        return web.json_response({
            "status": "protocol started",
            "total_time": total_duration,
            "round_times": round_times,
            "reply_count": self.reply_count
        })

    # REPLY 메시지 처리 (클라이언트)
    async def handle_reply(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("handle_reply: JSON parsing error")
            return web.json_response({"status": "error parsing reply"}, status=400)
        cluster_id = data.get("cluster_id")
        self.logger.info(f"[CLIENT] REPLY received from Cluster {cluster_id}: {data}")
        async with self.reply_condition:
            self.reply_count += 1
            self.reply_cluster_ids.append(cluster_id)
            self.reply_condition.notify_all()
        return web.json_response({"status": "reply received"})

    async def close(self):
        try:
            await self.session.close()
        except Exception:
            self.logger.exception("Error closing client session.")

##########################################################################
# LLAPBFT 노드 클래스 (드론 역할)
##########################################################################
class LLAPBFTNode:
    def __init__(self, index: int, config: dict, logger: logging.Logger, bandwidth_filepath: str = None):
        self.index = index
        self.config = config
        self.logger = logger
        self.node_info = config['drones'][index]
        self.f = config.get('f', 1)
        self.session = aiohttp.ClientSession(
            connector=TCPConnector(limit=0, force_close=False),
            timeout=aiohttp.ClientTimeout(total=9999)
        )
        self.bandwidth_data = load_bandwidth_data(bandwidth_filepath)
        self.statuses = {}
        self.cluster_info = None  # PRE-REQUEST 수신 시 설정

    # /ping 엔드포인트 핸들러 (사전 연결 지원)
    async def handle_ping(self, request: web.Request):
        return web.json_response({"status": "pong"})

    async def send_message(self, target: dict, endpoint: str, data: dict):
        url = f"http://{target['host']}:{target['port']}{endpoint}"
        try:
            await send_with_delay(self.session, self.node_info, target, url, data, self.bandwidth_data)
        except Exception as e:
            self.logger.exception(f"[{self.node_info['port']}] Error sending to {url}: {e}")

    async def broadcast(self, endpoint: str, message: dict):
        if not self.cluster_info:
            self.logger.info(f"[{self.node_info['port']}] Broadcast: Cluster information not set")
            return
        tasks = []
        for port in self.cluster_info["cluster_ports"]:
            if self.cluster_info.get("cluster_manager") and port == self.cluster_info["cluster_manager"]['port']:
                continue
            if port == self.node_info['port']:
                continue
            target = next((d for d in self.config['drones'] if d['port'] == port), None)
            if target:
                tasks.append(self.send_message(target, endpoint, message))
        await asyncio.gather(*tasks, return_exceptions=True)

    # REQUEST 엔드포인트 핸들러 (클러스터 매니저 및 리더 공용)
    async def handle_request(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("handle_request: JSON parsing error")
            return web.json_response({"status": "error parsing request"}, status=400)
        origin = data.get("origin")
        if origin == "client":
            # 클라이언트로부터의 PRE-REQUEST 수신: 자신이 클러스터 매니저 역할임
            cluster_ports = data.get("cluster_ports", [])
            if self.node_info['port'] not in cluster_ports:
                self.logger.info(f"[{self.node_info['port']}] PRE-REQUEST: Not a cluster member")
                return web.json_response({"status": "not in cluster"})
            self.cluster_info = {
                "cluster_id": data.get("cluster_id"),
                "cluster_ports": cluster_ports,
                "cluster_manager": self.node_info,  # 매니저로서 자신 지정
                "leader": None
            }
            self.logger.info(f"[CLUSTER MANAGER {self.node_info['port']}] PRE-REQUEST received: {data}")
            leader_port = data.get("leader")
            self.cluster_info["leader"] = next((d for d in self.config['drones'] if d['port'] == leader_port), None)
            if not self.cluster_info["leader"]:
                self.logger.error(f"[CLUSTER MANAGER {self.node_info['port']}] Leader {leader_port} not found")
                return web.json_response({"status": "leader not found"}, status=400)
            data["origin"] = "manager"
            data["cluster_manager"] = self.node_info
            url = f"http://{self.cluster_info['leader']['host']}:{self.cluster_info['leader']['port']}/request"
            self.logger.info(f"[CLUSTER MANAGER {self.node_info['port']}] Sending REQUEST to Leader {leader_port}")
            await send_with_delay(self.session, self.node_info, self.cluster_info["leader"], url, data, self.bandwidth_data)
            return web.json_response({"status": "PRE-REQUEST forwarded by manager"})
        elif origin == "manager":
            # 클러스터 리더가 전달받은 PRE-REQUEST 처리
            cluster_ports = data.get("cluster_ports", [])
            if self.node_info['port'] not in cluster_ports:
                self.logger.info(f"[{self.node_info['port']}] Forwarded PRE-REQUEST: Not a cluster member")
                return web.json_response({"status": "not in cluster"})
            self.cluster_info = {
                "cluster_id": data.get("cluster_id"),
                "cluster_ports": data.get("cluster_ports"),
                "cluster_manager": data.get("cluster_manager"),
                "leader": self.node_info   # 리더로서 자신 지정
            }
            self.logger.info(f"[CLUSTER LEADER {self.node_info['port']}] Forwarded PRE-REQUEST received: {data}")
            preprepare_msg = {
                "request_id": data.get("request_id"),
                "data": data.get("data"),
                "timestamp": time.time(),
                "leader": self.node_info['port'],
                "cluster_id": data.get("cluster_id"),
                "cluster_ports": self.cluster_info.get("cluster_ports"),
                "cluster_manager": self.cluster_info.get("cluster_manager")
            }
            req_id = data.get("request_id")
            self.statuses[req_id] = Status(self.f)
            self.statuses[req_id].phase = Status.PREPREPARED
            self.statuses[req_id].preprepare_msg = preprepare_msg
            self.logger.info(f"[CLUSTER LEADER {self.node_info['port']}] Broadcasting PRE-PREPARE: REQUEST {req_id}")
            await self.broadcast('/preprepare', preprepare_msg)
            return web.json_response({"status": "PRE-PREPARE broadcasted by leader"})
        else:
            self.logger.info(f"[{self.node_info['port']}] PRE-REQUEST: Unknown origin → {data}")
            return web.json_response({"status": "unknown origin"})

    async def handle_preprepare(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("handle_preprepare: JSON parsing error")
            return web.json_response({"status": "error parsing preprepare"}, status=400)
        req_id = data.get("request_id")
        self.logger.info(f"[{self.node_info['port']}] PRE-PREPARE received: {data}")
        if not self.cluster_info:
            self.cluster_info = {
                "cluster_id": data.get("cluster_id"),
                "cluster_ports": data.get("cluster_ports"),
                "cluster_manager": data.get("cluster_manager")
            }
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].preprepare_msg = data
        # 즉시 자신에 대한 PREPARE 메시지 추가 (자체 카운팅)
        self.statuses[req_id].add_prepare({
            "request_id": req_id,
            "data": data.get("data"),
            "timestamp": time.time(),
            "sender": self.node_info['port'],
            "cluster_id": data.get("cluster_id")
        })
        prepare_msg = {
            "request_id": req_id,
            "data": data.get("data"),
            "timestamp": time.time(),
            "sender": self.node_info['port'],
            "cluster_id": data.get("cluster_id")
        }
        self.logger.info(f"[{self.node_info['port']}] Broadcasting PREPARE: REQUEST {req_id}")
        await self.broadcast('/prepare', prepare_msg)
        return web.json_response({"status": "PREPARE broadcasted"})

    async def handle_prepare(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("handle_prepare: JSON parsing error")
            return web.json_response({"status": "error parsing prepare"}, status=400)
        req_id = data.get("request_id")
        sender = data.get("sender")
        self.logger.info(f"[{self.node_info['port']}] PREPARE received from {sender} for REQUEST {req_id}")
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].add_prepare(data)
        if self.statuses[req_id].preprepare_msg is None:
            self.logger.info(f"[{self.node_info['port']}] Missing PRE-PREPARE: Ignoring REQUEST {req_id}")
            return web.json_response({"status": "PREPARE ignored due to missing PRE-PREPARE"})
        if self.statuses[req_id].is_prepared() and self.statuses[req_id].phase != Status.PREPARED:
            self.statuses[req_id].phase = Status.PREPARED
            commit_msg = {
                "request_id": req_id,
                "data": self.statuses[req_id].preprepare_msg.get("data"),
                "timestamp": time.time(),
                "sender": self.node_info['port'],
                "cluster_id": self.cluster_info["cluster_id"] if self.cluster_info else None
            }
            self.statuses[req_id].add_commit(commit_msg)
            self.logger.info(f"[{self.node_info['port']}] Broadcasting COMMIT: REQUEST {req_id}")
            await self.broadcast('/commit', commit_msg)
        return web.json_response({"status": "PREPARE processed"})

    async def handle_commit(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("handle_commit: JSON parsing error")
            return web.json_response({"status": "error parsing commit"}, status=400)
        req_id = data.get("request_id")
        sender = data.get("sender")
        self.logger.info(f"[{self.node_info['port']}] COMMIT received from {sender} for REQUEST {req_id}")
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].add_commit(data)
        if self.statuses[req_id].is_committed() and not self.statuses[req_id].reply_sent:
            self.statuses[req_id].reply_sent = True
            if self.cluster_info:
                manager = self.cluster_info["cluster_manager"]
                prereply_msg = {
                    "request_id": req_id,
                    "data": self.statuses[req_id].preprepare_msg.get("data"),
                    "timestamp": time.time(),
                    "sender": self.node_info['port'],
                    "cluster_id": self.cluster_info["cluster_id"]
                }
                if self.node_info['port'] != manager['port']:
                    self.logger.info(f"[{self.node_info['port']}] COMMIT complete. Sending PRE-REPLY: REQUEST {req_id} directly to Manager {manager['port']}")
                    await self.send_message(manager, '/prereply', prereply_msg)
        return web.json_response({"status": "COMMIT processed"})

    async def handle_prereply(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("handle_prereply: JSON parsing error")
            return web.json_response({"status": "error parsing prereply"}, status=400)
        req_id = data.get("request_id")
        sender = data.get("sender")
        self.logger.info(f"[CLUSTER MANAGER {self.node_info['port']}] Received PRE-REPLY from {sender} for req_id {req_id}")
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].add_prereply(data)
        senders = [msg.get("sender") for msg in self.statuses[req_id].prereply_msgs]
        self.logger.info(f"[CLUSTER MANAGER {self.node_info['port']}] PRE-REPLY senders for req_id {req_id}: {senders}")
        if self.statuses[req_id].is_prereplied() and not self.statuses[req_id].reply_sent:
            self.logger.info(f"[CLUSTER MANAGER {self.node_info['port']}] Condition met for req_id {req_id} (f+1 messages).")
            self.statuses[req_id].reply_sent = True
            try:
                client = next(d for d in self.config['drones'] if int(d['port']) == 20001)
                self.logger.info(f"[CLUSTER MANAGER {self.node_info['port']}] Client found: {client}")
            except StopIteration:
                self.logger.error("Client (port 20001) not found!")
                return web.json_response({"status": "client not found"}, status=400)
            reply_msg = {
                "request_id": req_id,
                "data": self.statuses[req_id].preprepare_msg.get("data") if self.statuses[req_id].preprepare_msg else None,
                "timestamp": time.time(),
                "sender": self.node_info['port'],
                "cluster_id": self.cluster_info["cluster_id"] if self.cluster_info else None
            }
            self.logger.info(f"[CLUSTER MANAGER {self.node_info['port']}] Sending final REPLY for req_id {req_id} to Client {client['port']}.")
            try:
                await self.send_message(client, '/reply', reply_msg)
                self.logger.info(f"[CLUSTER MANAGER {self.node_info['port']}] Final REPLY for req_id {req_id} sent successfully.")
            except Exception as e:
                self.logger.exception(f"[CLUSTER MANAGER {self.node_info['port']}] Failed to send final REPLY for req_id {req_id}: {e}")
        return web.json_response({"status": "PRE-REPLY processed"})

    async def handle_reply(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("handle_reply (node): JSON parsing error")
            return web.json_response({"status": "error parsing reply"}, status=400)
        self.logger.info(f"[{self.node_info['port']}] REPLY received (log): {data}")
        return web.json_response({"status": "REPLY received"})

    async def close(self):
        try:
            await self.session.close()
        except Exception:
            self.logger.exception("Error closing node session.")

##########################################################################
# 설정 파일 로드 함수
##########################################################################
def load_config(config_filepath: str) -> dict:
    with open(config_filepath, 'r') as f:
        return yaml.safe_load(f)

##########################################################################
# 웹 서버 실행 헬퍼: 앱 실행 및 Runner 반환 (종료 시 cleanup용)
##########################################################################
async def serve_app(app: web.Application, host: str, port: int, logger: logging.Logger) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    return runner

##########################################################################
# 메인 함수: 역할(클라이언트 vs. 노드)에 따라 실행
##########################################################################
async def main():
    parser = argparse.ArgumentParser(description="LLAPBFT Simulation (Random Clustering)")
    parser.add_argument("--config", type=str, default="drone_info_control.yaml", help="Path to YAML configuration file")
    parser.add_argument("--index", type=int, required=True, help="Index in the drones list (client is port 20001)")
    parser.add_argument("--bandwidth", type=str, default="bandwidth_info.yaml", help="Path to bandwidth info YAML file")
    args = parser.parse_args()
    config = load_config(args.config)
    if config.get("protocol", "").lower() != "random_pbft":
        print("Error: Only lla_pbft protocol is supported.")
        return
    node = config["drones"][args.index]
    role = "client" if node["port"] == 20001 else "node"
    scenario_info = f"LLAPBFT Scenario: f={config.get('f')}, k={config.get('k')}"
    if role == "client":
        logger = setup_logging("Client", "client.log")
        logger.info(f"{scenario_info} | Role: CLIENT")
        client = LLAPBFTClient(config, logger, bandwidth_file=args.bandwidth)
        app = web.Application()
        app.add_routes([
            web.post('/reply', client.handle_reply),
            web.post('/start-protocol', client.start_protocol)
        ])
        app.add_routes([web.get('/ping', lambda request: web.json_response({"status": "pong"}))])
        client_addr = client.client
        runner = await serve_app(app, client_addr['host'], client_addr['port'], logger)
        logger.info("Client: Waiting for /start-protocol trigger (e.g., using curl)")
        while True:
            await asyncio.sleep(3600)
    else:
        logger = setup_logging(f"Node_{args.index}", f"node_{args.index}.log")
        logger.info(f"Node started: {node['host']}:{node['port']} | index={args.index} | f={config.get('f')}, k={config.get('k')}")
        node_instance = LLAPBFTNode(args.index, config, logger, bandwidth_filepath=args.bandwidth)
        app = web.Application()
        app.add_routes([
            web.post('/request', node_instance.handle_request),
            web.post('/preprepare', node_instance.handle_preprepare),
            web.post('/prepare', node_instance.handle_prepare),
            web.post('/commit', node_instance.handle_commit),
            web.post('/prereply', node_instance.handle_prereply),
            web.post('/reply', node_instance.handle_reply),
            web.get('/ping', node_instance.handle_ping)
        ])
        node_addr = node_instance.node_info
        runner = await serve_app(app, node_addr['host'], node_addr['port'], logger)
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