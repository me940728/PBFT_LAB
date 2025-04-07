#!/usr/bin/env python3
"""
RANDOM_PBFT 합의 시뮬레이션 코드 (랜덤 클러스터링 + 지연 적용, pre-reply 포함)
메시지 크기는 외부 메시지 파일 (message_<m>.json)을 읽어 전역변수로 관리하며,
클러스터 크기는 --cluster-size 인자로 override 할 수 있습니다.
"""

import asyncio
import aiohttp
from aiohttp import TCPConnector, web, AsyncResolver
import logging
import time
import argparse
import os
import yaml
import json
import sys
import numpy as np
import gc
import random
from common import calculate_distance, simulate_delay, get_message_bit_size

# RANDOM_PBFT 프로토콜이므로, 클러스터링은 무작위 방식으로 진행합니다.
if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 전역 메시지 크기는 main()에서 --message-size 인자로 설정됨.
GLOBAL_MESSAGE_SIZE_BITS = 0

##########################################################################
# 헬퍼 함수: 단순화된 로그 출력 (Phase, Receiver, Sender, 거리, 딜레이, 메시지 크기)
##########################################################################
def log_message_info(logger, phase, receiver, sender, distance, delay, size_bits):
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
# 유틸리티 함수: 메시지 전송 및 지연 적용
##########################################################################
async def send_with_delay(session: aiohttp.ClientSession, source: dict, target: dict, url: str, data: dict, bw_data: dict, padding_mb: int = 0):
    if bw_data:
        source_coords = (source.get('latitude', 0), source.get('longitude', 0), source.get('altitude', 0))
        target_coords = (target.get('latitude', 0), target.get('longitude', 0), target.get('altitude', 0))
        dist = calculate_distance(source_coords, target_coords)
        if dist >= 300:
            data["fault"] = True
            return "Fault: Message not delivered due to extreme distance"
        msg_size_bits = GLOBAL_MESSAGE_SIZE_BITS
        delay_str = await simulate_delay(dist, msg_size_bits, bw_data)
        try:
            delay_val = float(delay_str.rstrip('s'))
        except Exception:
            delay_val = 0.0
        data["simulated_delay"] = delay_str
        data["distance"] = dist
        data["message_size_bits"] = msg_size_bits
        log_message_info(
            logging.getLogger("DelayLogger"),
            phase="SEND",
            receiver=target.get("port", "N/A"),
            sender=source.get("port", "N/A"),
            distance=dist,
            delay=delay_str,
            size_bits=msg_size_bits
        )
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
    log_dir = os.path.join(os.getcwd(), "log", "random_pbft")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, log_filename)
    logger = logging.getLogger(logger_name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fmt = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        fh = logging.FileHandler(log_path, mode='a')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger

##########################################################################
# 유틸리티 함수: 대역폭 데이터 로드
##########################################################################
def load_bw_data(filepath: str) -> dict:
    if filepath and os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return None

##########################################################################
# 합의 상태 클래스 (pre-reply 포함)
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
##########################################################################
class RANDOMPBFTClient:
    def __init__(self, config: dict, logger: logging.Logger, bandwidth_file: str = None):
        self.config = config
        self.logger = logger
        # 클라이언트는 drones 리스트 중 port==20001
        self.client = next(d for d in config['drones'] if d['port'] == 20001)
        # 복제자(노드) 리스트
        self.drones = [d for d in config['drones'] if d['port'] != 20001]
        self.f = config.get('f', 1)
        # 클러스터 수는 config['k'] (혹은 --cluster-size 인자로 override됨)
        self.k = config.get('k', 3)
        self.padding_mb = config.get("m", 1)
        connector = TCPConnector(limit=0, force_close=False, resolver=AsyncResolver())
        self.session = aiohttp.ClientSession(connector=connector,
                                             timeout=aiohttp.ClientTimeout(total=9999))
        self.bw_data = load_bw_data(bandwidth_file)
        self.reply_events = {}  # {cluster_id: asyncio.Event()}
        self.reply_count = 0
        self.reply_cluster_ids = []
        self.reply_condition = asyncio.Condition()
        self.cluster_logger = setup_logging("Clustering", "clustering.log")
        self.clusters = self.perform_random_clustering()
        self.total_rounds = 1
        self.padded_payload = None

    def perform_random_clustering(self):
        """
        클러스터링을 무작위 방식으로 진행합니다.
        전체 복제자들을 무작위로 섞은 후, config['k'] 수 만큼 균등하게 분할합니다.
        각 클러스터에서 매니저와 리더는 무작위로 선택합니다.
        """
        drones = self.drones.copy()
        random.shuffle(drones)
        k = self.k
        clusters = []
        # 각 클러스터에 대해 드론을 분할 (균등 분할)
        # 만약 drones의 개수가 k로 나누어 떨어지지 않으면 마지막 클러스터에 나머지 포함
        cluster_lists = [drones[i::k] for i in range(k)]
        for cluster_id, cluster_drones in enumerate(cluster_lists):
            if not cluster_drones:
                continue
            # 무작위로 매니저 선출
            cluster_manager = random.choice(cluster_drones)
            # 매니저 제외한 리스트에서 리더 선출 (없으면 매니저가 리더)
            remaining = [d for d in cluster_drones if d != cluster_manager]
            cluster_leader = random.choice(remaining) if remaining else cluster_manager
            cluster_ports = [d['port'] for d in cluster_drones]
            self.cluster_logger.info(f"Cluster {cluster_id}: Manager: {cluster_manager['port']}, Leader: {cluster_leader['port']}, Members: {cluster_ports}")
            clusters.append({
                "cluster_id": cluster_id,
                "cluster_manager": cluster_manager,
                "cluster_leader": cluster_leader,
                "cluster_ports": cluster_ports
            })
        return clusters

    async def send_request_to_cluster(self, cluster, request_id):
        client_coords = (self.client['latitude'], self.client['longitude'], self.client['altitude'])
        manager = cluster["cluster_manager"]
        manager_coords = (manager['latitude'], manager['longitude'], manager['altitude'])
        distance_val = calculate_distance(client_coords, manager_coords)
        if distance_val >= 300:
            self.logger.info(f"[CLIENT] Excluding Cluster {cluster['cluster_id']}: distance to manager {manager['port']} is too high")
            return
        if self.padded_payload is None:
            self.padded_payload = {"latitude": self.client['latitude'],
                                   "longitude": self.client['longitude'],
                                   "altitude": self.client['altitude']}
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
        self.logger.info(f"[CLIENT] Sending PRE-REQUEST to Manager {manager['port']} for Cluster {cluster['cluster_id']} | Distance: {distance_val:7.2f} m | Size: {GLOBAL_MESSAGE_SIZE_BITS} bit")
        url = f"http://{manager['host']}:{manager['port']}/request"
        self.reply_events[cluster["cluster_id"]] = asyncio.Event()
        await send_with_delay(self.session, self.client, manager, url, data, self.bw_data)

    async def prewarm_connections(self):
        managers = [cluster["cluster_manager"] for cluster in self.clusters]
        tasks = []
        for manager in managers:
            url = f"http://{manager['host']}:{manager['port']}/ping"
            tasks.append(send_with_delay(self.session, self.client, manager, url, {"ping": True}, self.bw_data))
        await asyncio.gather(*tasks, return_exceptions=True)
        self.logger.info("Prewarm connections completed.")

    async def start_protocol(self, request: web.Request):
        payload = await request.json()
        self.logger.info(f"/start-protocol 입력값: {payload}")
        self.client['latitude'] = payload.get("latitude", self.client['latitude'])
        self.client['longitude'] = payload.get("longitude", self.client['longitude'])
        self.client['altitude'] = payload.get("altitude", self.client['altitude'])
        await self.prewarm_connections()
        self.padded_payload = payload
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
        leader_coords = (leader_info['latitude'], leader_info['longitude'], leader_info['altitude'])
        for replica in self.drones:
            replica_coords = (replica['latitude'], replica['longitude'], replica['altitude'])
            if calculate_distance(leader_coords, replica_coords) >= 300:
                fault_nodes.append(replica['port'])
        return web.json_response({
            "status": "protocol completed",
            "total_time": total_duration,
            "round_times": round_durations,
            "fault_nodes": fault_nodes,
            "protocol": {
                "name": "random_pbft",
                "rounds": self.total_rounds,
                "average_time": avg_duration
            }
        })

    async def handle_reply(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("handle_reply: JSON parsing error")
            return web.json_response({"status": "error parsing reply"}, status=400)
        cluster_id = data.get("cluster_id")
        log_message_info(
            self.logger,
            phase="REPLY",
            receiver=self.client['port'],
            sender=data.get("sender", "N/A"),
            distance=data.get("distance", 0),
            delay=data.get("simulated_delay", "N/A"),
            size_bits=GLOBAL_MESSAGE_SIZE_BITS
        )
        async with self.reply_condition:
            self.reply_count += 1
            self.reply_cluster_ids.append(cluster_id)
            self.reply_condition.notify_all()
        return web.json_response({"status": "reply received"})

    async def close(self):
        if self.session:
            await self.session.close()

##########################################################################
# 노드 클래스 (pre-reply 포함, 랜덤 PBFT)
##########################################################################
class RANDOMPBFTNode:
    def __init__(self, index: int, config: dict, logger: logging.Logger, bandwidth_filepath: str = None):
        self.index = index
        self.config = config
        self.logger = logger
        self.node_info = config['drones'][index]
        self.f = config.get('f', 1)
        self.padding_mb = config.get("m", 1)
        connector = TCPConnector(limit=0, force_close=False, resolver=AsyncResolver())
        self.session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=9999))
        self.bw_data = load_bw_data(bandwidth_filepath)
        self.statuses = {}
        self.cluster_info = None

    async def cleanup_finished_statuses(self):
        finished_requests = [req_id for req_id, status in self.statuses.items() if getattr(status, "finished", False)]
        for req_id in finished_requests:
            del self.statuses[req_id]
        gc.collect()
        self.logger.info(f"Cleaned up {len(finished_requests)} finished statuses")

    async def handle_ping(self, request: web.Request):
        return web.json_response({"status": "pong"})

    async def send_message(self, target: dict, endpoint: str, data: dict):
        url = f"http://{target['host']}:{target['port']}{endpoint}"
        try:
            await send_with_delay(self.session, self.node_info, target, url, data, self.bw_data, self.padding_mb)
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
                "cluster_manager": self.node_info,  # 이 노드가 클러스터 매니저 역할
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
                size_bits=GLOBAL_MESSAGE_SIZE_BITS
            )
            leader_port = data.get("leader")
            self.cluster_info["leader"] = next((d for d in self.config['drones'] if d['port'] == leader_port), None)
            if not self.cluster_info["leader"]:
                self.logger.error(f"[{self.node_info['port']}] Leader {leader_port} not found")
                return web.json_response({"status": "leader not found"}, status=400)
            data["origin"] = "manager"
            data["cluster_manager"] = self.node_info
            padded_data = data
            url = f"http://{self.cluster_info['leader']['host']}:{self.cluster_info['leader']['port']}/request"
            manager_coords = (self.cluster_info["cluster_manager"]['latitude'],
                              self.cluster_info["cluster_manager"]['longitude'],
                              self.cluster_info["cluster_manager"]['altitude'])
            leader = self.cluster_info["leader"]
            leader_coords = (leader['latitude'], leader['longitude'], leader['altitude'])
            msg_size_bits = GLOBAL_MESSAGE_SIZE_BITS
            distance_val = calculate_distance(manager_coords, leader_coords)
            delay_val = await simulate_delay(distance_val, msg_size_bits, self.bw_data)
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
                size_bits=msg_size_bits
            )
            await send_with_delay(self.session, self.node_info, leader, url, padded_data, self.bw_data, self.padding_mb)
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
                "leader": self.node_info  # 이 노드가 리더 역할
            }
            filtered_data = filter_dump_field(data)
            log_message_info(
                self.logger,
                phase="PRE-REQUEST-FWD",
                receiver=self.node_info['port'],
                sender=data.get("client_port", "N/A"),
                distance=0,
                delay="N/A",
                size_bits=GLOBAL_MESSAGE_SIZE_BITS
            )
            preprepare_msg = {
                "request_id": data.get("request_id"),
                "data": data.get("data"),
                "timestamp": time.time(),
                "leader": self.node_info['port'],
                "cluster_id": data.get("cluster_id"),
                "cluster_ports": self.cluster_info["cluster_ports"],
                "cluster_manager": self.cluster_info["cluster_manager"]
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
                size_bits=GLOBAL_MESSAGE_SIZE_BITS
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
            size_bits=GLOBAL_MESSAGE_SIZE_BITS
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
        # 자신의 dummy PREPARE 추가 (commit 전 단계)
        self.statuses[req_id].add_prepare({"dummy": True})
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
            size_bits=GLOBAL_MESSAGE_SIZE_BITS
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
        delay = data.get("simulated_delay", "N/A")
        distance = data.get("distance", 0.0)
        log_message_info(
            self.logger,
            phase="PREPARE",
            receiver=self.node_info['port'],
            sender=sender,
            distance=distance if isinstance(distance, (int, float)) else 0,
            delay=delay,
            size_bits=GLOBAL_MESSAGE_SIZE_BITS
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
                size_bits=GLOBAL_MESSAGE_SIZE_BITS
            )
            # pre-reply 단계: 클러스터 매니저가 아닌 노드는 pre-reply 메시지를 클러스터 매니저에게 전송
            if self.cluster_info and self.node_info['port'] != self.cluster_info["cluster_manager"]['port']:
                prereply_msg = {
                    "request_id": req_id,
                    "timestamp": time.time(),
                    "sender": self.node_info['port']
                }
                await self.send_message(self.cluster_info["cluster_manager"], '/prereply', prereply_msg)
            else:
                # 클러스터 매니저는 자신의 commit도 pre-reply로 집계
                self.statuses[req_id].add_prereply({"sender": self.node_info['port'], "dummy": True})
            # 기존 commit 브로드캐스트 유지
            await self.broadcast('/commit', commit_msg)
        return web.json_response({"status": "PREPARE processed"})

    async def handle_commit(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("handle_commit: JSON parsing error")
            return web.json_response({"status": "error parsing commit"}, status=400)
        req_id = data.get("request_id")
        sender = data.get("sender", 0)
        delay = data.get("simulated_delay", "N/A")
        distance = data.get("distance", 0.0)
        log_message_info(
            self.logger,
            phase="COMMIT",
            receiver=self.node_info['port'],
            sender=sender,
            distance=distance if isinstance(distance, (int, float)) else 0,
            delay=delay,
            size_bits=GLOBAL_MESSAGE_SIZE_BITS
        )
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].add_commit(data)
        if self.statuses[req_id].is_committed() and not self.statuses[req_id].reply_sent:
            if self.cluster_info and self.node_info['port'] == self.cluster_info["cluster_manager"]['port']:
                if self.statuses[req_id].is_prereplied():
                    self.statuses[req_id].reply_sent = True
                    try:
                        client = next(d for d in self.config['drones'] if d['port'] == 20001)
                        log_message_info(
                            self.logger,
                            phase="FINAL REPLY",
                            receiver=client['port'],
                            sender=self.node_info['port'],
                            distance=distance,
                            delay=delay,
                            size_bits=GLOBAL_MESSAGE_SIZE_BITS
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
                    self.statuses[req_id].finished = True
                    await self.cleanup_finished_statuses()
                    await asyncio.sleep(2)
            # 클러스터 매니저가 아닌 노드는 이미 pre-reply를 전송한 것으로 가정
        return web.json_response({"status": "COMMIT processed"})

    async def handle_reply_node(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("handle_reply (node): JSON parsing error")
            return web.json_response({"status": "error parsing reply"}, status=400)
        log_message_info(
            self.logger,
            phase="REPLY",
            receiver=self.node_info['port'],
            sender=data.get("sender", "N/A"),
            distance=data.get("distance", 0),
            delay=data.get("simulated_delay", "N/A"),
            size_bits=GLOBAL_MESSAGE_SIZE_BITS
        )
        return web.json_response({"status": "REPLY received"})

    async def handle_prereply(self, request: web.Request):
        try:
            data = await request.json()
        except Exception:
            self.logger.exception("handle_prereply: JSON parsing error")
            return web.json_response({"status": "error parsing prereply"}, status=400)
        req_id = data.get("request_id")
        sender = data.get("sender")
        log_message_info(
            self.logger,
            phase="PRE-REPLY",
            receiver=self.node_info['port'],
            sender=sender,
            distance=data.get("distance", 0),
            delay=data.get("simulated_delay", "N/A"),
            size_bits=GLOBAL_MESSAGE_SIZE_BITS
        )
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].add_prereply(data)
        self.logger.info(f"PRE-REPLY count for req_id {req_id}: {len(self.statuses[req_id].prereply_msgs)}")
        if self.statuses[req_id].is_prereplied() and not self.statuses[req_id].reply_sent:
            self.statuses[req_id].reply_sent = True
            try:
                client = next(d for d in self.config['drones'] if d['port'] == 20001)
                log_message_info(
                    self.logger,
                    phase="FINAL REPLY",
                    receiver=client['port'],
                    sender=self.node_info['port'],
                    distance=data.get("distance", 0),
                    delay=data.get("simulated_delay", "N/A"),
                    size_bits=GLOBAL_MESSAGE_SIZE_BITS
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
# 웹 애플리케이션 실행 헬퍼
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
    parser = argparse.ArgumentParser(description="RANDOMPBFT (RANDOM_PBFT) Simulation")
    parser.add_argument("--config", type=str, default="drone_info_control.yaml", help="Path to YAML configuration file")
    parser.add_argument("--index", type=int, required=True, help="Index in the drones list (client is port 20001)")
    parser.add_argument("--bandwidth", type=str, default="bandwidth_info.yaml", help="Path to bandwidth info YAML file")
    parser.add_argument("--message-size", type=int, default=1, help="Message size (MB) to choose message file (e.g., 1, 3, 5)")
    parser.add_argument("--cluster-size", type=int, default=None, help="Override cluster size (k) in config")
    args = parser.parse_args()
    # 쉘스크립트에서 전달받은 메시지 크기를 전역 변수로 설정
    global GLOBAL_MESSAGE_SIZE_BITS
    GLOBAL_MESSAGE_SIZE_BITS = get_message_bit_size(args.message_size)
    config = load_config(args.config)
    # 클러스터 크기 인자가 전달되면 config['k']를 override
    if args.cluster_size is not None:
        config['k'] = args.cluster_size
    if config.get("protocol", "").lower() != "random_pbft":
        print("Error: Only random_pbft protocol is supported.")
        return
    # 프로토콜 명칭을 RANDOM_PBFT로 변경하여 사용
    config["protocol"] = "random_pbft"
    node = config["drones"][args.index]
    role = "client" if node["port"] == 20001 else "node"
    scenario_info = f"RANDOM_PBFT Scenario: f={config.get('f')}, k={config.get('k')}"
    if role == "client":
        logger = setup_logging("Client", "client.log")
        logger.info(f"{scenario_info} | Role: CLIENT")
        client = RANDOMPBFTClient(config, logger, bandwidth_file=args.bandwidth)
        app = web.Application(client_max_size=10 * 1024 * 1024)
        app.add_routes([
            web.post('/reply', client.handle_reply),
            web.post('/start-protocol', client.start_protocol),
            web.get('/ping', lambda req: web.json_response({"status": "pong"}))
        ])
        client_addr = client.client
        runner = await serve_app(app, client_addr['host'], client_addr['port'], logger)
        logger.info("Client: Waiting for /start-protocol trigger")
        while True:
            await asyncio.sleep(3600)
    else:
        logger = setup_logging(f"Node_{args.index:3d}", f"node_{args.index}.log")
        logger.info(f"Node started: {node['host']}:{node['port']} | index={args.index:3d} | f={config.get('f')}, k={config.get('k')}")
        node_instance = RANDOMPBFTNode(args.index, config, logger, bandwidth_filepath=args.bandwidth)
        app = web.Application(client_max_size=10 * 1024 * 1024)
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
