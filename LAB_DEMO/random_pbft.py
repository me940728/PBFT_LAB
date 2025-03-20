#!/usr/bin/env python3
"""
랜덤 방식
import random

def perform_clustering(self):
    clusters = []
    # 드론 리스트 복사 후 무작위 섞기
    drone_list = self.drones.copy()
    random.shuffle(drone_list)
    # k개의 클러스터로 균등하게 분할 (리스트 슬라이싱 이용)
    clusters_data = [drone_list[i::self.k] for i in range(self.k)]
    for cluster_id, cluster_drones in enumerate(clusters_data):
        # 클러스터에 최소 하나의 드론이 있어야 함
        if not cluster_drones:
            continue
        # 클러스터 매니저를 무작위로 선택
        cluster_manager = random.choice(cluster_drones)
        # 매니저를 제외한 드론 목록
        remaining = [d for d in cluster_drones if d != cluster_manager]
        # 리더 선택: 남은 드론이 있다면 무작위로 선택, 없으면 매니저를 리더로 지정
        cluster_leader = random.choice(remaining) if remaining else cluster_manager
        cluster_ports = [d['port'] for d in cluster_drones]
        self.cluster_logger.info(
            f"Cluster {cluster_id}: Manager: {cluster_manager['port']}, Leader: {cluster_leader['port']}, Members: {cluster_ports}"
        )
        clusters.append({
            "cluster_id": cluster_id,
            "cluster_manager": cluster_manager,
            "cluster_leader": cluster_leader,
            "cluster_ports": cluster_ports
        })
    return clusters
만 변경
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
import numpy as np
from common import calculate_distance, simulate_delay, dump_message
import gc
import random

try:
    from sklearn.cluster import KMeans
except ImportError:
    print("scikit-learn is required. Please install it and run again.")
    sys.exit(1)

if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

##########################################################################
# 헬퍼 함수: 단순화된 로그 출력 (Phase, Receiver, Sender, 거리, 딜레이, 메시지 크기)
##########################################################################
def log_message_info(logger, phase, receiver, sender, distance, delay, size_bits):
    # distance를 7자리 고정폭(소수점 이하 2자리)로 포맷합니다.
    distance_str = f"{distance:7.2f}"
    logger.info(
        f"Phase: {phase} | Receiver: {receiver} | Sender: {sender} | "
        f"Distance: {distance_str} m | Delay: {delay} | Size: {size_bits} bit"
    )

##########################################################################
# 헬퍼 함수: 로그 출력 시 dump 필드 제거 (로그 노출 방지)
##########################################################################
def filter_dump_field(data: dict) -> dict:
    if not isinstance(data, dict):
        return data
    filtered = data.copy()
    filtered.pop("dump", None)
    return filtered

##########################################################################
# 유틸리티 함수: 메시지 전송 시 패딩 적용 (재패딩 금지)
##########################################################################
async def send_with_delay(session: aiohttp.ClientSession, source: dict, target: dict, url: str, data: dict, bandwidth_data: dict, padding_mb: int = 0):
    # 이미 "dump" 키가 있다면 이미 패딩된 것으로 간주 (재패딩 금지)
    if padding_mb and padding_mb > 0 and "dump" not in data:
        data = dump_message(data, padding_mb)
    if bandwidth_data:
        source_coords = (source.get('latitude', 0), source.get('longitude', 0), source.get('altitude', 0))
        target_coords = (target.get('latitude', 0), target.get('longitude', 0), target.get('altitude', 0))
        distance = calculate_distance(source_coords, target_coords)
        if distance >= 300:
            data["fault"] = True
            return "Fault: Message not delivered due to extreme distance"
        msg_size_bits = len(json.dumps(data).encode('utf-8')) * 8
        # simulate_delay는 딜레이 시간을 "초 s" 형식의 문자열로 반환합니다.
        delay_str = await simulate_delay(distance, msg_size_bits, bandwidth_data)
        # 문자열에서 "s"를 제거하고 실수형으로 변환합니다.
        delay_val = float(delay_str.rstrip('s'))
        data["simulated_delay"] = delay_str
        data["distance"] = distance
        data["message_size_bits"] = msg_size_bits
        log_message_info(
            logging.getLogger("DelayLogger"),
            phase="SEND",
            receiver=target.get("port", "N/A"),
            sender=source.get("port", "N/A"),
            distance=distance,
            delay=delay_str,
            size_bits=msg_size_bits
        )
        # 실제 딜레이 적용: 계산된 시간만큼 대기
        await asyncio.sleep(delay_val)
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
    log_dir = os.path.join(os.getcwd(), "log", "random")
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
# 클라이언트 클래스
# /start-protocol 요청 시 payload의 좌표 정보로 클라이언트 정보를 업데이트하고,
# 한 번 패딩된 원본 메시지를 생성한 후 모든 합의 단계에서 재사용합니다.
##########################################################################
class LLAPBFTClient:
    def __init__(self, config: dict, logger: logging.Logger, bandwidth_file: str = None):
        self.config = config
        self.logger = logger
        self.client = next(d for d in config['drones'] if d['port'] == 20001)
        self.drones = [d for d in config['drones'] if d['port'] != 20001]
        self.f = config.get('f', 1)
        self.k = config.get('k', 1)
        self.padding_mb = config.get("m", 1)
        self.session = aiohttp.ClientSession(
            connector=TCPConnector(limit=0, force_close=False),
            timeout=aiohttp.ClientTimeout(total=9999)
        )
        self.bandwidth_data = load_bandwidth_data(bandwidth_file)
        self.reply_events = {}
        self.reply_count = 0
        self.reply_cluster_ids = []
        self.reply_condition = asyncio.Condition()
        self.cluster_logger = setup_logging("Clustering", "clustering.log")
        self.clusters = self.perform_clustering()
        self.total_rounds = 3
        self.padded_payload = None

    # 이 부분만 변경 클러스터링 방식
    def perform_clustering(self):
        clusters = []
        # 드론 리스트 복사 후 무작위 섞기
        drone_list = self.drones.copy()
        random.shuffle(drone_list)
        # k개의 클러스터로 균등하게 분할 (리스트 슬라이싱 이용)
        clusters_data = [drone_list[i::self.k] for i in range(self.k)]
        for cluster_id, cluster_drones in enumerate(clusters_data):
            # 클러스터에 최소 하나의 드론이 있어야 함
            if not cluster_drones:
                continue
            # 클러스터 매니저를 무작위로 선택
            cluster_manager = random.choice(cluster_drones)
            # 매니저를 제외한 드론 목록
            remaining = [d for d in cluster_drones if d != cluster_manager]
            # 리더 선택: 남은 드론이 있다면 무작위로 선택, 없으면 매니저를 리더로 지정
            cluster_leader = random.choice(remaining) if remaining else cluster_manager
            cluster_ports = [d['port'] for d in cluster_drones]
            self.cluster_logger.info(
                f"Cluster {cluster_id}: Manager: {cluster_manager['port']}, Leader: {cluster_leader['port']}, Members: {cluster_ports}"
            )
            clusters.append({
                "cluster_id": cluster_id,
                "cluster_manager": cluster_manager,
                "cluster_leader": cluster_leader,
                "cluster_ports": cluster_ports
            })
        return clusters

    async def send_request_to_cluster(self, cluster, request_id):
        # 클라이언트→매니저: 클라이언트 좌표는 /start-protocol에서 업데이트됨.
        client_coords = (self.client.get('latitude', 0), self.client.get('longitude', 0), self.client.get('altitude', 0))
        manager = cluster["cluster_manager"]
        manager_coords = (manager.get('latitude', 0), manager.get('longitude', 0), manager.get('altitude', 0))
        # 클라이언트와 매니저 간 거리 계산
        distance_val = calculate_distance(client_coords, manager_coords)
        if distance_val >= 300:
            self.logger.info(f"[CLIENT] Excluding Cluster {cluster['cluster_id']}: distance to manager {manager['port']} is too high")
            return
        if self.padded_payload is None:
            base_payload = {"latitude": self.client.get("latitude"),
                            "longitude": self.client.get("longitude"),
                            "altitude": self.client.get("altitude")}
            self.padded_payload = dump_message(base_payload, self.padding_mb)
        # 메시지 구성: 패딩된 원본 메시지를 사용합니다.
        data = {
            "request_id": request_id,
            "timestamp": time.time(),
            "data": self.padded_payload,
            "cluster_id": cluster["cluster_id"],
            "cluster_ports": cluster["cluster_ports"],
            "leader": cluster["cluster_leader"]['port'],
            "client_port": self.client['port'],
            "origin": "client"
        }
        # 메시지 크기 계산
        msg_size_bits = len(json.dumps(data).encode('utf-8')) * 8
        # 딜레이 계산 (simulate_delay는 비동기 함수)
        delay_val = await simulate_delay(distance_val, msg_size_bits, self.bandwidth_data)
        # 추가 로그: 클라이언트와 매니저 간 거리, 딜레이, 메시지 크기
        self.logger.info(f"[CLIENT] Sending PRE-REQUEST to Manager {manager['port']} for Cluster {cluster['cluster_id']} | "
                        f"Distance: {distance_val:7.2f} m | Delay: {delay_val} | Size: {msg_size_bits} bit")
        # 메시지 전송
        url = f"http://{manager['host']}:{manager['port']}/request"
        self.reply_events[cluster["cluster_id"]] = asyncio.Event()
        await send_with_delay(self.session, self.client, manager, url, data, self.bandwidth_data, self.padding_mb)


    async def prewarm_connections(self):
        managers = [cluster["cluster_manager"] for cluster in self.clusters]
        tasks = []
        for manager in managers:
            url = f"http://{manager['host']}:{manager['port']}/ping"
            tasks.append(send_with_delay(self.session, self.client, manager, url, {"ping": True}, self.bandwidth_data, self.padding_mb))
        await asyncio.gather(*tasks, return_exceptions=True)
        self.logger.info("Prewarm connections completed.")

    async def start_protocol(self, request: web.Request):
        payload = await request.json()
        self.logger.info(f"/start-protocol 입력값: {payload}")
        # 클라이언트 좌표 업데이트: 실제 좌표로 갱신
        self.client['latitude'] = payload.get("latitude", self.client.get("latitude", 0))
        self.client['longitude'] = payload.get("longitude", self.client.get("longitude", 0))
        self.client['altitude'] = payload.get("altitude", self.client.get("altitude", 0))
        await self.prewarm_connections()
        self.padded_payload = dump_message(payload, self.padding_mb)
        dummy_req_id = int(time.time() * 1000)
        await self.send_request_to_cluster(self.clusters[0], dummy_req_id)
        total_start = time.time()
        round_durations = []
        for rnd in range(1, self.total_rounds + 1):
            round_start = time.time()
            req_id = int(time.time() * 1000)
            tasks = [self.send_request_to_cluster(cluster, req_id) for cluster in self.clusters]
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(1)
            duration = time.time() - round_start
            round_durations.append(duration)
            self.logger.info(f"Round {rnd}: {duration:.4f} seconds")
            self.reply_events.clear()
            gc.collect()
            await asyncio.sleep(2)
        total_sleep = self.total_rounds * 2
        total_duration = (time.time() - total_start) - total_sleep
        avg_duration = sum(round_durations) / len(round_durations)
        self.logger.info(f"[][][][]===> CONSENSUS COMPLETED in {total_duration:.4f} seconds, Average Round: {avg_duration:.4f} seconds")
        fault_nodes = []
        leader_info = sorted(self.drones, key=lambda d: d['port'])[0]
        leader_coords = (leader_info.get('latitude', 0), leader_info.get('longitude', 0), leader_info.get('altitude', 0))
        for replica in self.drones:
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
            data = await request.json()
        except Exception:
            self.logger.exception("handle_reply: JSON parsing error")
            return web.json_response({"status": "error parsing reply"}, status=400)
        cluster_id = data.get("cluster_id")
        size_bits = len(json.dumps(data).encode('utf-8')) * 8
        delay = data.get("simulated_delay", "N/A")
        distance = data.get("distance", "N/A")
        log_message_info(
            self.logger,
            phase="REPLY",
            receiver=self.client['port'],
            sender=data.get("sender", "N/A"),
            distance=distance if isinstance(distance, (int, float)) else 0,
            delay=delay,
            size_bits=size_bits
        )
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
# 노드 클래스 (복제자 역할; 고정폭 로그 출력)
##########################################################################
class LLAPBFTNode:
    def __init__(self, index: int, config: dict, logger: logging.Logger, bandwidth_filepath: str = None):
        self.index = index
        self.config = config
        self.logger = logger
        self.node_info = config['drones'][index]
        self.f = config.get('f', 1)
        self.padding_mb = config.get("m", 1)
        self.session = aiohttp.ClientSession(
            connector=TCPConnector(limit=0, force_close=False),
            timeout=aiohttp.ClientTimeout(total=9999)
        )
        self.bandwidth_data = load_bandwidth_data(bandwidth_filepath)
        self.statuses = {}
        self.cluster_info = None

    async def handle_ping(self, request: web.Request):
        return web.json_response({"status": "pong"})

    async def send_message(self, target: dict, endpoint: str, data: dict):
        url = f"http://{target['host']}:{target['port']}{endpoint}"
        try:
            await send_with_delay(self.session, self.node_info, target, url, data, self.bandwidth_data, self.padding_mb)
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

    async def handle_request(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("handle_request: JSON parsing error")
            return web.json_response({"status": "error parsing request"}, status=400)
        origin = data.get("origin")
        if origin == "client":
            cluster_ports = data.get("cluster_ports", [])
            if self.node_info['port'] not in cluster_ports:
                self.logger.info(f"[{self.node_info['port']}] PRE-REQUEST: Not a cluster member")
                return web.json_response({"status": "not in cluster"})
            self.cluster_info = {
                "cluster_id": data.get("cluster_id"),
                "cluster_ports": cluster_ports,
                "cluster_manager": self.node_info,  # 현재 노드가 클러스터 매니저
                "leader": None
            }
            filtered_data = filter_dump_field(data)
            log_message_info(
                self.logger,
                phase="PRE-REQUEST",
                receiver=self.node_info['port'],
                sender=data.get("client_port", "N/A"),
                distance=0,
                delay="N/A",
                size_bits=len(json.dumps(data).encode('utf-8')) * 8
            )
            leader_port = data.get("leader")
            self.cluster_info["leader"] = next((d for d in self.config['drones'] if d['port'] == leader_port), None)
            if not self.cluster_info["leader"]:
                self.logger.error(f"[{self.node_info['port']}] Leader {leader_port} not found")
                return web.json_response({"status": "leader not found"}, status=400)
            data["origin"] = "manager"
            data["cluster_manager"] = self.node_info
            # 패딩된 메시지 생성 (재패딩 금지)
            padded_data = dump_message(data, self.padding_mb) if self.padding_mb and "dump" not in data else data
            url = f"http://{self.cluster_info['leader']['host']}:{self.cluster_info['leader']['port']}/request"
            # 거리 계산: 클러스터 매니저와 리더 간 좌표 사용
            manager_coords = (self.cluster_info["cluster_manager"].get('latitude', 0),
                              self.cluster_info["cluster_manager"].get('longitude', 0),
                              self.cluster_info["cluster_manager"].get('altitude', 0))
            leader = self.cluster_info["leader"]
            leader_coords = (leader.get('latitude', 0), leader.get('longitude', 0), leader.get('altitude', 0))
            message_size_bits = len(json.dumps(padded_data).encode('utf-8')) * 8
            distance_val = calculate_distance(manager_coords, leader_coords)
            delay_val = await simulate_delay(distance_val, message_size_bits, self.bandwidth_data)
            # sender: 원래 클라이언트 포트와 포워딩한 매니저 드론 포트 모두 기재
            orig_sender = data.get("client_port", "N/A")
            fwdr = self.node_info['port']
            sender_str = f"{orig_sender}(FWDR: {fwdr})"
            log_message_info(
                self.logger,
                phase="REQUEST",
                receiver=leader['port'],
                sender=sender_str,
                distance=distance_val,
                delay=delay_val,
                size_bits=message_size_bits
            )
            await send_with_delay(self.session, self.node_info, leader, url, padded_data, self.bandwidth_data, self.padding_mb)
            return web.json_response({"status": "PRE-REQUEST forwarded by manager"})
        elif origin == "manager":
            cluster_ports = data.get("cluster_ports", [])
            if self.node_info['port'] not in cluster_ports:
                self.logger.info(f"[{self.node_info['port']}] Forwarded PRE-REQUEST: Not a cluster member")
                return web.json_response({"status": "not in cluster"})
            self.cluster_info = {
                "cluster_id": data.get("cluster_id"),
                "cluster_ports": data.get("cluster_ports"),
                "cluster_manager": data.get("cluster_manager"),
                "leader": self.node_info   # 리더 역할
            }
            filtered_data = filter_dump_field(data)
            log_message_info(
                self.logger,
                phase="PRE-REQUEST-FWD",
                receiver=self.node_info['port'],
                sender=data.get("client_port", "N/A"),
                distance=0,
                delay="N/A",
                size_bits=len(json.dumps(data).encode('utf-8')) * 8
            )
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
            log_message_info(
                self.logger,
                phase="PRE-PREPARE",
                receiver="ALL",
                sender=self.node_info['port'],
                distance=0,
                delay="N/A",
                size_bits=len(json.dumps(preprepare_msg).encode('utf-8')) * 8
            )
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
        filtered_data = filter_dump_field(data)
        log_message_info(
            self.logger,
            phase="PRE-PREPARE",
            receiver=self.node_info['port'],
            sender=data.get("leader", "N/A"),
            distance=data.get("distance", 0),
            delay=data.get("simulated_delay", "N/A"),
            size_bits=len(json.dumps(data).encode('utf-8')) * 8
        )
        if not self.cluster_info:
            self.cluster_info = {
                "cluster_id": data.get("cluster_id"),
                "cluster_ports": data.get("cluster_ports"),
                "cluster_manager": data.get("cluster_manager")
            }
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].preprepare_msg = data
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
        log_message_info(
            self.logger,
            phase="PREPARE-BROADCAST",
            receiver="ALL",
            sender=self.node_info['port'],
            distance=data.get("distance", 0),
            delay=data.get("simulated_delay", "N/A"),
            size_bits=len(json.dumps(prepare_msg).encode('utf-8')) * 8
        )
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
        size_bits = len(json.dumps(data).encode('utf-8')) * 8
        delay = data.get("simulated_delay", "N/A")
        distance = data.get("distance", 0)
        receiver_port = self.node_info['port']
        log_message_info(
            self.logger,
            phase="PREPARE",
            receiver=receiver_port,
            sender=sender,
            distance=distance if isinstance(distance, (int, float)) else 0,
            delay=delay,
            size_bits=size_bits
        )
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].add_prepare(data)
        if self.statuses[req_id].preprepare_msg is None:
            self.logger.info(f"PREPARE: Missing PRE-PREPARE for REQUEST {req_id}")
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
            log_message_info(
                self.logger,
                phase="COMMIT-BROADCAST",
                receiver="ALL",
                sender=self.node_info['port'],
                distance=distance if isinstance(distance, (int, float)) else 0,
                delay=delay,
                size_bits=len(json.dumps(commit_msg).encode('utf-8')) * 8
            )
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
        size_bits = len(json.dumps(data).encode('utf-8')) * 8
        delay = data.get("simulated_delay", "N/A")
        distance = data.get("distance", 0)
        log_message_info(
            self.logger,
            phase="COMMIT",
            receiver=self.node_info['port'],
            sender=sender,
            distance=distance if isinstance(distance, (int, float)) else 0,
            delay=delay,
            size_bits=size_bits
        )
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
                    log_message_info(
                        self.logger,
                        phase="PRE-REPLY",
                        receiver=manager['port'],
                        sender=self.node_info['port'],
                        distance=distance if isinstance(distance, (int, float)) else 0,
                        delay=delay,
                        size_bits=len(json.dumps(prereply_msg).encode('utf-8')) * 8
                    )
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
        size_bits = len(json.dumps(data).encode('utf-8')) * 8
        delay = data.get("simulated_delay", "N/A")
        distance = data.get("distance", 0)
        log_message_info(
            self.logger,
            phase="PRE-REPLY",
            receiver=self.node_info['port'],
            sender=sender,
            distance=distance if isinstance(distance, (int, float)) else 0,
            delay=delay,
            size_bits=size_bits
        )
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].add_prereply(data)
        senders = [msg.get("sender") for msg in self.statuses[req_id].prereply_msgs]
        self.logger.info(f"PRE-REPLY senders for req_id {req_id}: {senders}")
        if self.statuses[req_id].is_prereplied() and not self.statuses[req_id].reply_sent:
            self.statuses[req_id].reply_sent = True
            try:
                client = next(d for d in self.config['drones'] if int(d['port']) == 20001)
                log_message_info(
                    self.logger,
                    phase="FINAL REPLY",
                    receiver=client['port'],
                    sender=self.node_info['port'],
                    distance=distance if isinstance(distance, (int, float)) else 0,
                    delay=delay,
                    size_bits=len(json.dumps(data).encode('utf-8')) * 8
                )
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
            await self.send_message(client, '/reply', reply_msg)
        return web.json_response({"status": "PRE-REPLY processed"})

    async def handle_reply_node(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("handle_reply (node): JSON parsing error")
            return web.json_response({"status": "error parsing reply"}, status=400)
        size_bits = len(json.dumps(data).encode('utf-8')) * 8
        delay = data.get("simulated_delay", "N/A")
        distance = data.get("distance", 0)
        log_message_info(
            self.logger,
            phase="REPLY",
            receiver=self.node_info['port'],
            sender=data.get("sender", "N/A"),
            distance=distance if isinstance(distance, (int, float)) else 0,
            delay=delay,
            size_bits=size_bits
        )
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
# 웹 서버 실행 헬퍼
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
    parser = argparse.ArgumentParser(description="LLAPBFT Simulation")
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
        app = web.Application(client_max_size=10*1024*1024)
        app.add_routes([
            web.post('/reply', client.handle_reply),
            web.post('/start-protocol', client.start_protocol),
            web.get('/ping', lambda request: web.json_response({"status": "pong"}))
        ])
        client_addr = client.client
        runner = await serve_app(app, client_addr['host'], client_addr['port'], logger)
        logger.info("Client: Waiting for /start-protocol trigger")
        while True:
            await asyncio.sleep(3600)
    else:
        logger = setup_logging(f"Node_{args.index:3d}", f"node_{args.index}.log")
        logger.info(f"Node started: {node['host']}:{node['port']} | index={args.index:3d} | f={config.get('f')}, k={config.get('k')}")
        node_instance = LLAPBFTNode(args.index, config, logger, bandwidth_filepath=args.bandwidth)
        app = web.Application(client_max_size=10*1024*1024)
        app.add_routes([
            web.post('/request', node_instance.handle_request),
            web.post('/preprepare', node_instance.handle_preprepare),
            web.post('/prepare', node_instance.handle_prepare),
            web.post('/commit', node_instance.handle_commit),
            web.post('/prereply', node_instance.handle_prereply),
            web.post('/reply', node_instance.handle_reply_node),
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