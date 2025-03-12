#!/usr/bin/env python3
"""
LLAPBFT Consensus Simulation Code (with random clustering - 2D version)
- The client randomly splits the drones (excluding the client) into k clusters based on YAML.
- Clustering is performed using only the 2D coordinates (latitude and longitude).
- In each cluster:
    * The cluster manager is selected as the drone closest to the client (2D distance).
    * The cluster center is computed as the average of latitude and longitude of all drones in the cluster.
    * The cluster leader is selected as the drone (excluding the manager) closest to the cluster center (2D distance).
- (후속 메시지 전송 등 LLAPBFT 프로토콜 관련 나머지 로직은 동일합니다.)
- Clustering information is logged in a separate log file (clustering.log).
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
from common import calculate_distance, simulate_delay  # 기존 3D 거리 계산 함수 등
import random
import numpy as np

try:
    from sklearn.cluster import KMeans
except ImportError:
    print("scikit-learn is required. Please install it and run again.")
    sys.exit(1)

if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

##########################################################################
# Helper function: Send message with simulated delay (same as in pbft)
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
# Logging setup
##########################################################################
def setup_logging(name: str, log_file_name: str) -> logging.Logger:
    log_dir = os.path.join(os.getcwd(), "log", "llpbft")
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
# Load bandwidth data
##########################################################################
def load_bandwidth_data(bandwidth_file: str) -> dict:
    if bandwidth_file and os.path.exists(bandwidth_file):
        with open(bandwidth_file, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return None

##########################################################################
# Consensus status class (similar to PBFT)
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
# RandomLLAPBFTClient class (Random Clustering Version - 2D)
##########################################################################
class RandomLLAPBFTClient:
    def __init__(self, config: dict, logger: logging.Logger, bandwidth_file: str = None):
        self.config = config
        self.logger = logger
        # Client is the node with port 20001
        self.client = next(d for d in config['drones'] if d['port'] == 20001)
        # All other drones (candidate nodes)
        self.drones = [d for d in config['drones'] if d['port'] != 20001]
        self.f = config.get('f', 1)
        self.k = config.get('k', 1)  # k는 YAML에 정의된 값 사용
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=9999))
        self.bandwidth_data = load_bandwidth_data(bandwidth_file)
        self.reply_events = {}
        self.replies_received = 0
        self.reply_count = 0
        self.reply_cluster_ids = []
        self.reply_condition = asyncio.Condition()
        self.cluster_logger = setup_logging("Clustering", "clustering.log")
        # Perform random clustering using 2D coordinates (latitude and longitude only)
        self.clusters = self.perform_random_clustering()

    def perform_random_clustering(self):
        """
        Random clustering using 2D coordinates:
         - 각 드론에게 0부터 k-1까지 무작위 클러스터 ID를 할당합니다.
         - 각 클러스터의 센터는 위도와 경도의 평균으로 계산합니다.
         - 클러스터 매니저는 클라이언트와의 2D 거리가 가장 짧은 드론으로 선정합니다.
         - 매니저를 제외한 드론 중 클러스터 센터와의 2D 거리가 가장 짧은 드론을 리더로 선정합니다.
        """
        # 2D 거리 계산 함수 (위도, 경도만 사용)
        distance_2d = lambda p, q: np.linalg.norm(np.array(p) - np.array(q))
        
        # 드론 리스트 복사 및 무작위 클러스터 할당
        drones_copy = self.drones.copy()
        for d in drones_copy:
            d['cluster_id'] = random.randint(0, self.k - 1)
        clusters_dict = {}
        for d in drones_copy:
            cid = d['cluster_id']
            clusters_dict.setdefault(cid, []).append(d)
        
        clusters = []
        for cluster_id, group in clusters_dict.items():
            if not group:
                continue
            # 2D 클러스터 센터: 위도와 경도의 평균
            avg_lat = sum(d['latitude'] for d in group) / len(group)
            avg_lng = sum(d['longitude'] for d in group) / len(group)
            center = (avg_lat, avg_lng)
            # 클라이언트의 2D 좌표
            client_coords_2d = (self.client['latitude'], self.client['longitude'])
            # 클러스터 내 각 드론의 2D 좌표 및 클라이언트와의 거리 계산
            distances_to_client = [distance_2d(client_coords_2d, (d['latitude'], d['longitude'])) for d in group]
            manager_index = int(np.argmin(distances_to_client))
            cluster_manager = group[manager_index]
            # 매니저 제외한 드론 중 2D 클러스터 센터와의 거리 계산
            remaining = [d for d in group if d != cluster_manager]
            if remaining:
                distances_to_center = [distance_2d((d['latitude'], d['longitude']), center) for d in remaining]
                leader_index = int(np.argmin(distances_to_center))
                cluster_leader = remaining[leader_index]
            else:
                cluster_leader = cluster_manager
            cluster_ports = [d['port'] for d in group]
            self.cluster_logger.info(f"Random Cluster {cluster_id}: Manager: {cluster_manager['port']}, Leader: {cluster_leader['port']}, Members: {cluster_ports}")
            clusters.append({
                "cluster_id": cluster_id,
                "cluster_manager": cluster_manager,
                "cluster_leader": cluster_leader,
                "cluster_ports": cluster_ports,
                "center": center
            })
        return clusters

    async def send_request_to_cluster(self, cluster, request_id):
        if len(cluster["cluster_ports"]) < (3 * self.f + 1):
            self.logger.info(f"[CLIENT] Excluding Cluster {cluster['cluster_id']}: insufficient drones (required: {3*self.f+1}, current: {len(cluster['cluster_ports'])})")
            return
        client_coords = (self.client['latitude'], self.client['longitude'])
        manager = cluster["cluster_manager"]
        manager_coords = (manager['latitude'], manager['longitude'])
        # 2D 거리 계산
        distance = np.linalg.norm(np.array(client_coords) - np.array(manager_coords))
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
        self.logger.info(f"[CLIENT] Sending PRE-REQUEST to Cluster Manager {manager['port']} for Random Cluster {cluster['cluster_id']}")
        event = asyncio.Event()
        self.reply_events[cluster["cluster_id"]] = event
        await send_with_delay(self.session, self.client, manager, url, data, self.bandwidth_data)

    async def start_protocol(self, request: web.Request):
        self.logger.info("Precomputed cluster information available. Starting consensus round (Random Clustering - 2D)")
        total_start_time = time.time()
        request_id = int(time.time() * 1000)
        tasks = [self.send_request_to_cluster(cluster, request_id) for cluster in self.clusters]
        await asyncio.gather(*tasks, return_exceptions=True)
        self.logger.info("All PRE-REQUEST messages sent → Waiting for REPLY from cluster managers")
        required_replies = self.f + 1
        async with self.reply_condition:
            await self.reply_condition.wait_for(lambda: self.reply_count >= required_replies)
        self.logger.info(f"[CLIENT] REPLY received from Clusters {self.reply_cluster_ids}")
        total_duration = time.time() - total_start_time
        self.logger.info(f"[][][][] LLAPBFT Consensus completed: Total time = {total_duration:.4f} seconds")
        return web.json_response({
            "status": "protocol started",
            "total_time": total_duration,
            "reply_count": self.reply_count
        })

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
# RandomLLAPBFTNode class (Drone role, unchanged)
##########################################################################
class RandomLLAPBFTNode:
    def __init__(self, index: int, config: dict, logger: logging.Logger, bandwidth_file: str = None):
        self.index = index
        self.config = config
        self.logger = logger
        self.node_info = config['drones'][index]
        self.f = config.get('f', 1)
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=9999))
        self.bandwidth_data = load_bandwidth_data(bandwidth_file)
        self.statuses = {}
        self.cluster_info = None

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
                "cluster_manager": self.node_info,
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
            cluster_ports = data.get("cluster_ports", [])
            if self.node_info['port'] not in cluster_ports:
                self.logger.info(f"[{self.node_info['port']}] Forwarded PRE-REQUEST: Not a cluster member")
                return web.json_response({"status": "not in cluster"})
            self.cluster_info = {
                "cluster_id": data.get("cluster_id"),
                "cluster_ports": data.get("cluster_ports"),
                "cluster_manager": data.get("cluster_manager"),
                "leader": self.node_info
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
            self.logger.exception("Error closing client session.")

##########################################################################
# Load configuration file
##########################################################################
def load_config(path: str) -> dict:
    with open(path, 'r') as f:
        return yaml.safe_load(f)

##########################################################################
# Web server runner helper: Start app and return runner (for cleanup)
##########################################################################
async def serve_app(app: web.Application, host: str, port: int, logger: logging.Logger) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    return runner

##########################################################################
# Main function: Run as client or node based on role
##########################################################################
async def main():
    parser = argparse.ArgumentParser(description="LLAPBFT Simulation")
    parser.add_argument("--config", type=str, default="drone_info_control.yaml", help="Path to YAML configuration file")
    parser.add_argument("--index", type=int, required=True, help="Index in the drones list (client is port 20001)")
    parser.add_argument("--bandwidth", type=str, default="bandwidth_info.yaml", help="Path to bandwidth info YAML file")
    args = parser.parse_args()
    config = load_config(args.config)
    if config.get("protocol", "").lower() != "ll_pbft":
        print("Error: Only ll_pbft protocol is supported.")
        return
    node = config["drones"][args.index]
    role = "client" if node["port"] == 20001 else "node"
    scenario_info = f"LLAPBFT Scenario: f={config.get('f')}, k={config.get('k')}"
    if role == "client":
        logger = setup_logging("Client", "client.log")
        logger.info(f"{scenario_info} | Role: CLIENT")
        client = RandomLLAPBFTClient(config, logger, bandwidth_file=args.bandwidth)
        app = web.Application()
        app.add_routes([
            web.post('/reply', client.handle_reply),
            web.post('/start-protocol', client.start_protocol)
        ])
        client_addr = client.client
        runner = await serve_app(app, client_addr['host'], client_addr['port'], logger)
        logger.info("Client: Waiting for /start-protocol trigger (e.g., using curl)")
        while True:
            await asyncio.sleep(3600)
    else:
        logger = setup_logging(f"Node_{args.index}", f"node_{args.index}.log")
        logger.info(f"Node started: {node['host']}:{node['port']} | index={args.index} | f={config.get('f')}, k={config.get('k')}")
        node_instance = RandomLLAPBFTNode(args.index, config, logger, bandwidth_file=args.bandwidth)
        app = web.Application()
        app.add_routes([
            web.post('/request', node_instance.handle_request),
            web.post('/preprepare', node_instance.handle_preprepare),
            web.post('/prepare', node_instance.handle_prepare),
            web.post('/commit', node_instance.handle_commit),
            web.post('/prereply', node_instance.handle_prereply),
            web.post('/reply', node_instance.handle_reply)
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
