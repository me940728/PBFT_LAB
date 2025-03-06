#!/usr/bin/env python3
"""
리팩터링된 LLAPBFT 합의 시뮬레이션 코드
- 공통 네트워크 전송 및 fault 체크 로직을 BaseEntity 클래스로 분리
- 상태 관리, 로깅, 타임아웃 처리를 개선하여 코드 중복을 줄임
- 클라이언트와 노드 역할별 핸들러는 공통 기능을 재사용함
"""

import asyncio, aiohttp, logging, time, argparse, os, yaml, json
from aiohttp import web
from common import calculate_distance, simulate_delay
from sklearn.cluster import KMeans
import numpy as np

##########################################################################
# 공통 유틸리티 함수 및 클래스
##########################################################################
def setup_logging(name: str, log_file_name: str) -> logging.Logger:
    log_dir = os.path.join(os.getcwd(), "log", "llapbft")
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, log_file_name)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')

    # 중복 핸들러 추가 방지
    if not logger.handlers:
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        logger.addHandler(ch)

        fh = logging.FileHandler(log_file_path, mode='a')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger

def load_config(path: str) -> dict:
    with open(path, 'r') as f:
        return yaml.safe_load(f)

def perform_clustering(client_location, drones, k):
    """
    클라이언트 위치와 드론 리스트를 바탕으로 KMeans 클러스터링을 수행하고,
    각 클러스터에서 매니저, 리더, 멤버 정보를 포함하는 리스트를 반환합니다.
    """
    if len(drones) < k:
        k = len(drones)
    coords = np.array([[d['latitude'], d['longitude'], d['altitude']] for d in drones])
    kmeans = KMeans(n_clusters=k, random_state=0).fit(coords)
    labels = kmeans.labels_
    centroids = kmeans.cluster_centers_
    
    clusters = []
    for cluster_id in range(k):
        cluster_members = [d for d, label in zip(drones, labels) if label == cluster_id]
        if not cluster_members:
            continue

        healthy_members = [d for d in cluster_members if not d.get("fault", False)]
        if healthy_members:
            manager = min(healthy_members, key=lambda d: calculate_distance(
                (d['latitude'], d['longitude'], d['altitude']),
                client_location))
        else:
            manager = min(cluster_members, key=lambda d: calculate_distance(
                (d['latitude'], d['longitude'], d['altitude']),
                client_location))
        
        non_manager = [d for d in cluster_members if d['port'] != manager['port']]
        healthy_non_manager = [d for d in non_manager if not d.get("fault", False)]
        leader = min(healthy_non_manager, key=lambda d: calculate_distance(
                (d['latitude'], d['longitude'], d['altitude']),
                centroids[cluster_id])) if healthy_non_manager else manager

        clusters.append({
            "manager": manager,
            "leader": leader,
            "members": cluster_members
        })
    return clusters

def log_cluster_info(clusters):
    log_dir = os.path.join(os.getcwd(), "log", "llapbft")
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, "cluster_info.log")
    with open(log_file_path, "a") as f:
        f.write("\n========== Cluster Info @ {} ==========\n".format(time.strftime("%Y-%m-%d %H:%M:%S")))
        for idx, cluster in enumerate(clusters):
            f.write(f"Cluster {idx+1}:\n")
            f.write(f"  Manager: Port {cluster['manager']['port']}, Location ({cluster['manager']['latitude']}, {cluster['manager']['longitude']}, {cluster['manager']['altitude']})\n")
            f.write(f"  Leader: Port {cluster['leader']['port']}, Location ({cluster['leader']['latitude']}, {cluster['leader']['longitude']}, {cluster['leader']['altitude']})\n")
            f.write("  Members:\n")
            for m in cluster['members']:
                f.write(f"    - Port {m['port']}, Location ({m['latitude']}, {m['longitude']}, {m['altitude']})\n")
        f.write("============================================\n")

class BaseEntity:
    """
    네트워크 메시지 전송과 공통 fault flag 체크를 수행하는 기본 클래스.
    클라이언트, 노드 모두에서 상속받아 사용.
    """
    def __init__(self, entity_info, bandwidth_file: str = None):
        self.entity_info = entity_info
        self.session = aiohttp.ClientSession()
        self.bandwidth_data = None
        if bandwidth_file and os.path.exists(bandwidth_file):
            with open(bandwidth_file, 'r', encoding='utf-8') as f:
                self.bandwidth_data = yaml.safe_load(f)

    async def send_message(self, target, endpoint, data, logger: logging.Logger):
        url = f"http://{target['host']}:{target['port']}{endpoint}"
        try:
            response = await send_with_delay(self.session, self.entity_info, target, url, data, self.bandwidth_data)
            return response
        except Exception as e:
            logger.error(f"[Entity {self.entity_info['port']}] Error sending to {url}: {e}")

    async def close(self):
        await self.session.close()

##########################################################################
# 공통 메시지 전송 함수 (send_with_delay)
##########################################################################
async def send_with_delay(session, source, target, url, data, bandwidth_data):
    if bandwidth_data:
        source_coords = (source.get('latitude', 0), source.get('longitude', 0), source.get('altitude', 0))
        target_coords = (target.get('latitude', 0), target.get('longitude', 0), target.get('altitude', 0))
        distance = calculate_distance(source_coords, target_coords)
        message_size_bits = len(json.dumps(data)) * 8
        delay = await simulate_delay(distance, message_size_bits, bandwidth_data)
        delay_logger = logging.getLogger("DelayLogger")
        delay_logger.info(f"[DELAY] 송신: Port{source['port']} → 수신: Port{target['port']} | 거리: {distance:.2f} m | 메시지 크기: {message_size_bits} bit | 지연: {delay:.4f} sec")
        data["simulated_delay"] = delay
        data["distance"] = distance
        data["message_size_bits"] = message_size_bits
        data["fault"] = True if delay == 0 else False
    async with session.post(url, json=data) as resp:
        return await resp.text()

##########################################################################
# 상태 관리 클래스
##########################################################################
class LLAPBFTStatus:
    """
    합의 프로토콜의 각 단계를 추적하며 메시지를 저장합니다.
    단계: PRE_REQUEST → REQUEST → PRE_PREPARE → PREPARE → COMMIT → PRE_REPLY → REPLY
    """
    def __init__(self, f):
        self.f = f
        self.phase = "PRE_REQUEST"
        self.msgs = {}
        self.cluster_info = None
        self.reply_received = False

    def update_phase(self, new_phase):
        self.phase = new_phase

    def add_message(self, phase, msg):
        self.msgs.setdefault(phase, []).append(msg)

##########################################################################
# LLAPBFT Client 클래스
##########################################################################
class LLAPBFTClient(BaseEntity):
    def __init__(self, config, logger, bandwidth_file=None):
        client_info = next(d for d in config['drones'] if d['port'] == 20001)
        super().__init__(client_info, bandwidth_file)
        self.config = config
        self.logger = logger
        self.drones = [d for d in config['drones'] if d['port'] != 20001]
        self.f = config.get('f', 1)
        self.k = config.get('k', 2)
        self.total_rounds = 1  # 합의 라운드 수
        self.reply_counts = {}  # {request_id: count}

    async def send_request_to_cluster_manager(self, cluster, request_data):
        manager = cluster['manager']
        url = f"http://{manager['host']}:{manager['port']}/request"
        data = {
            "request_id": request_data["request_id"],
            "phase": "REQUEST",
            "client_data": request_data,
            "cluster_info": {
                "manager": cluster['manager'],
                "leader": cluster['leader'],
                "members": cluster['members']
            }
        }
        self.logger.info(f"[CLIENT] >> REQUEST {data['request_id']} to Cluster Manager (Port {manager['port']})")
        await self.send_message(manager, '/request', data, self.logger)

    async def start_protocol(self, request):
        req_json = await request.json()
        client_location = (req_json.get("latitude"), req_json.get("longitude"), req_json.get("altitude"))
        self.logger.info("★★★ /start-protocol 요청 수신: LLAPBFT 합의 라운드를 시작합니다. ★★★")
        total_consensus_time = 0.0
        for round_number in range(1, self.total_rounds + 1):
            clusters = perform_clustering(client_location, self.drones, self.k)
            log_cluster_info(clusters)
            
            round_start_time = time.time()
            req_id = int(time.time() * 1000)
            req_data = {
                "request_id": req_id,
                "timestamp": time.time(),
                "data": f"message_{req_id}",
                "client_location": client_location
            }
            self.reply_counts[req_id] = 0

            await asyncio.gather(*(self.send_request_to_cluster_manager(cluster, req_data) for cluster in clusters))
            
            # f+1개의 REPLY가 도착하는 순간까지 기다림
            try:
                await asyncio.wait_for(self.wait_for_replies(req_id), timeout=30)
                consensus_time = time.time() - round_start_time
                self.logger.info("★★★ CONSENSUS COMPLETED: Total Consensus Time = {:.4f} seconds ★★★".format(consensus_time))
            except asyncio.TimeoutError:
                self.logger.error("타임아웃: 충분한 REPLY 메시지를 받지 못했습니다.")
                consensus_time = time.time() - round_start_time
                self.logger.info("★★★ CONSENSUS COMPLETED (타임아웃): Total Consensus Time = {:.4f} seconds ★★★".format(consensus_time))

            # 안정화 대기 (합의 시간 측정에는 포함하지 않음)
            await asyncio.sleep(5)
            total_consensus_time += consensus_time

            self.logger.info(f"[CLIENT] 라운드 {round_number} 완료: 처리 시간 = {consensus_time:.4f} seconds")
            
        self.logger.info("★★★ 전체 합의 완료: Total Consensus Time = {:.4f} seconds ★★★".format(total_consensus_time))
        return web.json_response({"status": "protocol started", "total_time": total_consensus_time})

    async def wait_for_replies(self, req_id):
        while self.reply_counts.get(req_id, 0) < (self.f + 1):
            await asyncio.sleep(0.5)

    async def handle_reply(self, request):
        data = await request.json()
        if data.get("fault", False):
            self.logger.info(f"[CLIENT] Ignoring FAULT reply: {data}")
            return web.json_response({"status": "Fault reply ignored"})
        req_id = data.get("request_id")
        self.reply_counts.setdefault(req_id, 0)
        self.reply_counts[req_id] += 1
        self.logger.info(f"[CLIENT] REPLY: {data} (현재 REPLY 수: {self.reply_counts[req_id]})")
        return web.json_response({"status": "reply received"})

##########################################################################
# LLAPBFT Node 클래스
##########################################################################
class LLAPBFTNode(BaseEntity):
    def __init__(self, index, config, logger, bandwidth_file=None):
        node_info = config['drones'][index]
        super().__init__(node_info, bandwidth_file)
        self.index = index
        self.config = config
        self.logger = logger
        self.f = config.get('f', 1)
        self.statuses = {}

    def get_status(self, req_id):
        if req_id not in self.statuses:
            self.statuses[req_id] = LLAPBFTStatus(self.f)
        return self.statuses[req_id]

    async def handle_request(self, request):
        data = await request.json()
        if data.get("fault", False):
            self.logger.info(f"[NODE {self.entity_info['port']}] Ignoring FAULT REQUEST: {data.get('request_id')}")
            return web.json_response({"status": "Ignored fault request"})
        req_id = data.get("request_id")
        cluster_info = data.get("cluster_info")
        self.logger.info(f"[NODE {self.entity_info['port']}] Received REQUEST {req_id} with cluster_info: Manager {cluster_info['manager']['port']}, Leader {cluster_info['leader']['port']}")
        if self.entity_info['port'] == cluster_info['manager']['port']:
            status = self.get_status(req_id)
            status.cluster_info = cluster_info
            leader = cluster_info['leader']
            self.logger.info(f"[CLUSTER MANAGER {self.entity_info['port']}] Forwarding REQUEST {req_id} to Cluster Leader (Port {leader['port']})")
            await self.send_message(leader, '/request_leader', data, self.logger)
            return web.json_response({"status": "REQUEST forwarded to leader"})
        else:
            self.logger.warning(f"[NODE {self.entity_info['port']}] Received REQUEST but not acting as Cluster Manager")
            return web.json_response({"status": "not cluster manager"})

    async def handle_request_leader(self, request):
        data = await request.json()
        if data.get("fault", False):
            self.logger.info(f"[NODE {self.entity_info['port']}] Ignoring FAULT REQUEST_LEADER: {data.get('request_id')}")
            return web.json_response({"status": "Ignored fault request_leader"})
        req_id = data.get("request_id")
        cluster_info = data.get("cluster_info")
        self.logger.info(f"[NODE {self.entity_info['port']}] (Cluster Leader) Received REQUEST {req_id}")
        status = self.get_status(req_id)
        status.cluster_info = cluster_info
        preprepare_msg = {
            "request_id": req_id,
            "phase": "PRE_PREPARE",
            "data": data.get("client_data"),
            "timestamp": time.time(),
            "sender": self.entity_info['port'],
            "cluster_info": cluster_info
        }
        status.update_phase("PRE_PREPARE")
        self.logger.info(f"[CLUSTER LEADER {self.entity_info['port']}] Broadcasting PRE-PREPARE for REQUEST {req_id}")
        await asyncio.gather(*(self.send_message(member, '/preprepare', preprepare_msg, self.logger)
                               for member in cluster_info['members']
                               if member['port'] not in [cluster_info['manager']['port'], self.entity_info['port']]))
        return web.json_response({"status": "PRE-PREPARE broadcasted"})

    async def handle_preprepare(self, request):
        data = await request.json()
        if data.get("fault", False):
            self.logger.info(f"[NODE {self.entity_info['port']}] Ignoring FAULT PRE-PREPARE for REQUEST {data.get('request_id')}")
            return web.json_response({"status": "Ignored fault preprepare"})
        req_id = data.get("request_id")
        cluster_info = data.get("cluster_info")
        self.logger.info(f"[NODE {self.entity_info['port']}] Received PRE-PREPARE for REQUEST {req_id}")
        status = self.get_status(req_id)
        status.update_phase("PREPARE")
        prepare_msg = {
            "request_id": req_id,
            "phase": "PREPARE",
            "data": data.get("data"),
            "timestamp": time.time(),
            "sender": self.entity_info['port'],
            "cluster_info": cluster_info
        }
        self.logger.info(f"[NODE {self.entity_info['port']}] Broadcasting PREPARE for REQUEST {req_id}")
        await asyncio.gather(*(self.send_message(member, '/prepare', prepare_msg, self.logger)
                               for member in cluster_info['members']
                               if member['port'] not in [cluster_info['manager']['port'], self.entity_info['port']]))
        return web.json_response({"status": "PREPARE broadcasted"})

    async def handle_prepare(self, request):
        data = await request.json()
        if data.get("fault", False):
            self.logger.info(f"[NODE {self.entity_info['port']}] Ignoring FAULT PREPARE for REQUEST {data.get('request_id')}")
            return web.json_response({"status": "Ignored fault prepare"})
        req_id = data.get("request_id")
        cluster_info = data.get("cluster_info")
        self.logger.info(f"[NODE {self.entity_info['port']}] Received PREPARE for REQUEST {req_id} from {data.get('sender')}")
        status = self.get_status(req_id)
        status.add_message("PREPARE", data)
        if len(status.msgs.get("PREPARE", [])) >= (2 * self.f) and status.phase != "COMMIT":
            status.update_phase("COMMIT")
            commit_msg = {
                "request_id": req_id,
                "phase": "COMMIT",
                "data": data.get("data"),
                "timestamp": time.time(),
                "sender": self.entity_info['port'],
                "cluster_info": cluster_info
            }
            self.logger.info(f"[NODE {self.entity_info['port']}] Broadcasting COMMIT for REQUEST {req_id}")
            await asyncio.gather(*(self.send_message(member, '/commit', commit_msg, self.logger)
                                   for member in cluster_info['members']
                                   if member['port'] not in [cluster_info['manager']['port'], self.entity_info['port']]))
        return web.json_response({"status": "PREPARE processed"})

    async def handle_commit(self, request):
        data = await request.json()
        if data.get("fault", False):
            self.logger.info(f"[NODE {self.entity_info['port']}] Ignoring FAULT COMMIT for REQUEST {data.get('request_id')}")
            return web.json_response({"status": "Ignored fault commit"})
        req_id = data.get("request_id")
        cluster_info = data.get("cluster_info")
        self.logger.info(f"[NODE {self.entity_info['port']}] Received COMMIT for REQUEST {req_id} from {data.get('sender')}")
        status = self.get_status(req_id)
        status.add_message("COMMIT", data)
        if len(status.msgs.get("COMMIT", [])) >= (2 * self.f + 1) and status.phase != "PRE_REPLY":
            status.update_phase("PRE_REPLY")
            pre_reply_msg = {
                "request_id": req_id,
                "phase": "PRE_REPLY",
                "data": data.get("data"),
                "timestamp": time.time(),
                "sender": self.entity_info['port'],
                "cluster_info": cluster_info
            }
            manager = cluster_info['manager']
            self.logger.info(f"[NODE {self.entity_info['port']}] Sending PRE_REPLY for REQUEST {req_id} to Cluster Manager (Port {manager['port']})")
            await self.send_message(manager, '/pre_reply', pre_reply_msg, self.logger)
        return web.json_response({"status": "COMMIT processed"})

    async def handle_pre_reply(self, request):
        data = await request.json()
        if data.get("fault", False):
            self.logger.info(f"[NODE {self.entity_info['port']}] Ignoring FAULT PRE_REPLY for REQUEST {data.get('request_id')}")
            return web.json_response({"status": "Ignored fault pre_reply"})
        req_id = data.get("request_id")
        self.logger.info(f"[CLUSTER MANAGER {self.entity_info['port']}] Received PRE_REPLY for REQUEST {req_id} from {data.get('sender')}")
        status = self.get_status(req_id)
        status.add_message("PRE_REPLY", data)
        if len(status.msgs.get("PRE_REPLY", [])) >= (self.f + 1) and not status.reply_received:
            status.reply_received = True
            reply_msg = {
                "request_id": req_id,
                "phase": "REPLY",
                "data": data.get("data"),
                "timestamp": time.time(),
                "sender": self.entity_info['port']
            }
            client = next(d for d in self.config['drones'] if d['port'] == 20001)
            self.logger.info(f"[CLUSTER MANAGER {self.entity_info['port']}] Sending REPLY for REQUEST {req_id} to Client (Port {client['port']})")
            await self.send_message(client, '/reply', reply_msg, self.logger)
        return web.json_response({"status": "PRE_REPLY processed"})

    async def handle_reply(self, request):
        data = await request.json()
        if data.get("fault", False):
            self.logger.info(f"[NODE {self.entity_info['port']}] Ignoring FAULT REPLY (for logging): {data}")
            return web.json_response({"status": "Ignored fault reply"})
        self.logger.info(f"[NODE {self.entity_info['port']}] Received REPLY (for logging): {data}")
        return web.json_response({"status": "REPLY received"})

##########################################################################
# 메인 함수: 역할에 따라 클라이언트 또는 노드 서버 실행
##########################################################################
async def main():
    parser = argparse.ArgumentParser(description="LLAPBFT Simulation")
    parser.add_argument("--config", type=str, default="drone_info_control.yaml", help="Path to YAML configuration file")
    parser.add_argument("--index", type=int, required=True, help="Index in the drones list; 클라이언트는 port 20001")
    parser.add_argument("--bandwidth", type=str, default="bandwidth_info.yaml", help="Path to bandwidth info YAML file")
    args = parser.parse_args()

    config = load_config(args.config)
    protocol = config.get("protocol", "").lower()
    if protocol != "llapbft":
        print("Error: Only llapbft protocol is supported in this simulation.")
        return

    node = config["drones"][args.index]
    role = "client" if node["port"] == 20001 else "node"

    if role == "client":
        logger = setup_logging("LLAPBFT_Client", "client.log")
        logger.info(f"LLAPBFT Scenario: f={config.get('f')}, k={config.get('k')} | Role: CLIENT")
        client = LLAPBFTClient(config, logger, bandwidth_file=args.bandwidth)
        app = web.Application()
        app.add_routes([
            web.post('/reply', client.handle_reply),
            web.post('/start-protocol', client.start_protocol)
        ])
        runner = web.AppRunner(app)
        await runner.setup()
        client_addr = client.entity_info
        site = web.TCPSite(runner, host=client_addr['host'], port=client_addr['port'])
        await site.start()
        logger.info(f"[CLIENT] Server started at http://{client_addr['host']}:{client_addr['port']}")
        logger.info("Waiting for /start-protocol trigger (use curl to start consensus)...")
        while True:
            await asyncio.sleep(3600)
        await client.close()
        await runner.cleanup()
    else:
        logger = setup_logging(f"LLAPBFT_Node_{args.index}", f"node_{args.index}.log")
        logger.info(f"[NODE] Starting node at {node['host']}:{node['port']} | index={args.index:3} | f={config.get('f')}, k={config.get('k')}")
        node_instance = LLAPBFTNode(args.index, config, logger, bandwidth_file=args.bandwidth)
        app = web.Application()
        app.add_routes([
            web.post('/request', node_instance.handle_request),
            web.post('/request_leader', node_instance.handle_request_leader),
            web.post('/preprepare', node_instance.handle_preprepare),
            web.post('/prepare', node_instance.handle_prepare),
            web.post('/commit', node_instance.handle_commit),
            web.post('/pre_reply', node_instance.handle_pre_reply),
            web.post('/reply', node_instance.handle_reply),
        ])
        runner = web.AppRunner(app)
        await runner.setup()
        node_addr = node_instance.entity_info
        site = web.TCPSite(runner, host=node_addr['host'], port=node_addr['port'])
        await site.start()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Shutting down node.")
        await node_instance.close()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
