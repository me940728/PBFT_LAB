#!/usr/bin/env python3
"""
llapbft.py

LLAPBFT 합의 시뮬레이션 코드
- 확장된 합의 프로토콜: PRE_REQUEST, REQUEST, PRE_PREPARE, PREPARE, COMMIT, PRE_REPLY, REPLY
- 클라이언트(DCC, port 20001)는 모든 드론(복제자)로부터 위치 정보를 받아, scikit-learn KMeans를 통해 클러스터링을 수행합니다.
- 각 클러스터에서는 클라이언트와 거리가 가장 가까운 드론을 클러스터 매니저로, 클러스터 중심에 가장 가까운 드론(매니저 제외)을 클러스터 리더로 선출합니다.
- 클러스터링 결과는 log/cluster_info.log 에 저장되며, 가독성 높은 형식으로 기록됩니다.
- 이후 클라이언트는 각 클러스터 매니저에게 REQUEST 메시지를 보내고, 클러스터 매니저는 메시지를 클러스터 리더에게 전달합니다.
- 클러스터 리더는 PRE_PREPARE를 시작으로, 클러스터 내부(클러스터 매니저 제외)에서 합의를 진행하고, 최종적으로 클러스터 매니저를 통해 클라이언트에게 REPLY를 전달합니다.
"""

import asyncio, aiohttp, logging, time, argparse, os, yaml, json
from aiohttp import web
from common import calculate_distance, simulate_delay

# scikit-learn KMeans와 numpy 임포트
from sklearn.cluster import KMeans
import numpy as np

##########################################################################
# 클러스터링 및 로그 기록 (PRE-REQUEST 단계)
##########################################################################
def perform_clustering(client_location, drones, k):
    """
    client_location: (lat, lon, alt)
    drones: 클러스터링 대상 드론 리스트 (포트 20001 제외)
    k: 클러스터 수
    반환: 클러스터링 결과 리스트. 각 클러스터는 dict 형식으로
        {
            "manager": drone dict,   # 클라이언트에 가장 가까운 드론
            "leader": drone dict,    # 클러스터 중심에 가장 가까운 드론 (manager 제외)
            "members": [drone dict, ...]  # 클러스터에 속한 모든 드론
        }
    """
    if len(drones) < k:
        k = len(drones)
    # 3차원 좌표 배열 구성
    coords = np.array([[d['latitude'], d['longitude'], d['altitude']] for d in drones])
    kmeans = KMeans(n_clusters=k, random_state=0).fit(coords)
    labels = kmeans.labels_
    centroids = kmeans.cluster_centers_
    
    clusters = []
    for cluster_id in range(k):
        cluster_members = [d for d, label in zip(drones, labels) if label == cluster_id]
        if not cluster_members:
            continue
        # 클라이언트 위치와의 거리를 기준으로 클러스터 매니저 선정
        manager = min(cluster_members, key=lambda d: calculate_distance(
            (d['latitude'], d['longitude'], d['altitude']),
            client_location))
        # 클러스터 중심과의 거리를 기준으로 리더 선정 (manager 제외)
        non_manager = [d for d in cluster_members if d['port'] != manager['port']]
        leader = min(non_manager, key=lambda d: calculate_distance(
            (d['latitude'], d['longitude'], d['altitude']),
            centroids[cluster_id])) if non_manager else manager
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

##########################################################################
# 로깅 설정
##########################################################################
def setup_logging(name, log_file_name):
    log_dir = os.path.join(os.getcwd(), "log", "llapbft")
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, log_file_name)
    
    logger = logging.getLogger(name)
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
# 확장된 Status 클래스 (7단계 추적)
##########################################################################
class LLAPBFTStatus:
    # 단계: PRE_REQUEST → REQUEST → PRE_PREPARE → PREPARE → COMMIT → PRE_REPLY → REPLY
    def __init__(self, f):
        self.f = f
        self.phase = "PRE_REQUEST"
        self.msgs = {}  # 각 단계별 메시지 저장
        self.cluster_info = None  # {"manager":..., "leader":..., "members":...}
        self.reply_received = False

    def update_phase(self, new_phase):
        self.phase = new_phase

    def add_message(self, phase, msg):
        if phase not in self.msgs:
            self.msgs[phase] = []
        self.msgs[phase].append(msg)

##########################################################################
# LLAPBFT Client 클래스 (DCC, port 20001)
##########################################################################
class LLAPBFTClient:
    def __init__(self, config, logger, bandwidth_file=None):
        self.config = config
        self.logger = logger
        # 클라이언트: drones 리스트 중 port가 20001
        self.client = next(d for d in config['drones'] if d['port'] == 20001)
        # 합의 대상 드론: port 20001 제외
        self.drones = [d for d in config['drones'] if d['port'] != 20001]
        self.f = config.get('f', 1)
        self.k = config.get('k', 2)  # 클러스터 개수 (필요에 따라 수정)
        self.session = aiohttp.ClientSession()
        self.bandwidth_file = bandwidth_file
        if self.bandwidth_file and os.path.exists(self.bandwidth_file):
            with open(self.bandwidth_file, 'r') as f:
                self.bandwidth_data = yaml.safe_load(f)
        else:
            self.bandwidth_data = None
        self.total_rounds = 2  # 합의 라운드 수
        # 각 라운드별 REPLY 카운트 저장
        self.reply_counts = {}

    async def send_request_to_cluster_manager(self, cluster, request_data):
        """
        클러스터 매니저에게 REQUEST 메시지 전송.
        메시지에는 cluster_info (manager, leader, members)와 클라이언트의 위치 및 원본 데이터 포함.
        """
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
        await send_with_delay(self.session, self.client, manager, url, data, self.bandwidth_data)

    async def start_protocol(self, request):
        req_json = await request.json()
        client_location = (req_json.get("latitude"), req_json.get("longitude"), req_json.get("altitude"))
        self.logger.info("★★★ /start-protocol 요청 수신: LLAPBFT 합의 라운드를 시작합니다. ★★★")
        total_processing_time = 0.0  # 대기 시간 제외한 전체 처리 시간
        for round_number in range(1, self.total_rounds + 1):
            # PRE-REQUEST: 클러스터링 수행
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

            # 각 클러스터 매니저에게 REQUEST 메시지 전송
            await asyncio.gather(*(self.send_request_to_cluster_manager(cluster, req_data) for cluster in clusters))

            # REPLY 대기 (f+1개의 REPLY 수신 또는 타임아웃)
            timeout = 30  # seconds
            start_wait = time.time()
            while self.reply_counts[req_id] < (self.f + 1):
                await asyncio.sleep(0.5)
                if time.time() - start_wait > timeout:
                    self.logger.error("타임아웃: 충분한 REPLY 메시지를 받지 못했습니다.")
                    break

            round_end_time = time.time()
            round_processing_time = round_end_time - round_start_time
            total_processing_time += round_processing_time

            self.logger.info(f"[CLIENT] 라운드 {round_number} 완료: 처리 시간 = {round_processing_time:.4f} seconds")
            await asyncio.sleep(5)  # 안정화 대기 (측정 제외)

        self.logger.info("★★★ CONSENSUS COMPLETED: Total Consensus Time = {:.4f} seconds ★★★".format(total_processing_time))
        return web.json_response({"status": "protocol started", "total_time": total_processing_time})

    async def handle_reply(self, request):
        data = await request.json()
        req_id = data.get("request_id")
        self.reply_counts.setdefault(req_id, 0)
        self.reply_counts[req_id] += 1
        self.logger.info(f"[CLIENT] REPLY: {data} (현재 REPLY 수: {self.reply_counts[req_id]})")
        return web.json_response({"status": "reply received"})

    async def close(self):
        await self.session.close()

##########################################################################
# LLAPBFT Node 클래스 (모든 드론: 역할은 메시지에 따라 동적 결정)
##########################################################################
class LLAPBFTNode:
    def __init__(self, index, config, logger, bandwidth_file=None):
        self.index = index
        self.config = config
        self.logger = logger
        self.node_info = config['drones'][index]
        self.f = config.get('f', 1)
        self.session = aiohttp.ClientSession()
        self.bandwidth_file = bandwidth_file
        if self.bandwidth_file and os.path.exists(self.bandwidth_file):
            with open(self.bandwidth_file, 'r') as f:
                self.bandwidth_data = yaml.safe_load(f)
        else:
            self.bandwidth_data = None
        # 각 요청에 대한 상태를 저장 (request_id -> LLAPBFTStatus)
        self.statuses = {}

    def get_status(self, req_id):
        if req_id not in self.statuses:
            self.statuses[req_id] = LLAPBFTStatus(self.f)
        return self.statuses[req_id]

    async def send_message(self, target, endpoint, data):
        url = f"http://{target['host']}:{target['port']}{endpoint}"
        try:
            await send_with_delay(self.session, self.node_info, target, url, data, self.bandwidth_data)
        except Exception as e:
            self.logger.error(f"[NODE {self.node_info['port']}] Error sending to {url}: {e}")

    async def handle_request(self, request):
        data = await request.json()
        req_id = data.get("request_id")
        cluster_info = data.get("cluster_info")
        self.logger.info(f"[NODE {self.node_info['port']}] Received REQUEST {req_id} with cluster_info: Manager {cluster_info['manager']['port']}, Leader {cluster_info['leader']['port']}")
        if self.node_info['port'] == cluster_info['manager']['port']:
            status = self.get_status(req_id)
            status.cluster_info = cluster_info
            leader = cluster_info['leader']
            url = f"http://{leader['host']}:{leader['port']}/request_leader"
            self.logger.info(f"[CLUSTER MANAGER {self.node_info['port']}] Forwarding REQUEST {req_id} to Cluster Leader (Port {leader['port']})")
            await send_with_delay(self.session, self.node_info, leader, url, data, self.bandwidth_data)
            return web.json_response({"status": "REQUEST forwarded to leader"})
        else:
            self.logger.warning(f"[NODE {self.node_info['port']}] Received REQUEST but not acting as Cluster Manager")
            return web.json_response({"status": "not cluster manager"})

    async def handle_request_leader(self, request):
        data = await request.json()
        req_id = data.get("request_id")
        cluster_info = data.get("cluster_info")
        self.logger.info(f"[NODE {self.node_info['port']}] (Cluster Leader) Received REQUEST {req_id}")
        status = self.get_status(req_id)
        status.cluster_info = cluster_info
        preprepare_msg = {
            "request_id": req_id,
            "phase": "PRE_PREPARE",
            "data": data.get("client_data"),
            "timestamp": time.time(),
            "sender": self.node_info['port'],
            "cluster_info": cluster_info
        }
        status.update_phase("PRE_PREPARE")
        self.logger.info(f"[CLUSTER LEADER {self.node_info['port']}] Broadcasting PRE-PREPARE for REQUEST {req_id}")
        await asyncio.gather(*(self.send_message(member, '/preprepare', preprepare_msg)
                               for member in cluster_info['members']
                               if member['port'] not in [cluster_info['manager']['port'], self.node_info['port']]))
        return web.json_response({"status": "PRE-PREPARE broadcasted"})

    async def handle_preprepare(self, request):
        data = await request.json()
        req_id = data.get("request_id")
        cluster_info = data.get("cluster_info")
        self.logger.info(f"[NODE {self.node_info['port']}] Received PRE-PREPARE for REQUEST {req_id}")
        status = self.get_status(req_id)
        status.update_phase("PREPARE")
        prepare_msg = {
            "request_id": req_id,
            "phase": "PREPARE",
            "data": data.get("data"),
            "timestamp": time.time(),
            "sender": self.node_info['port'],
            "cluster_info": cluster_info
        }
        self.logger.info(f"[NODE {self.node_info['port']}] Broadcasting PREPARE for REQUEST {req_id}")
        await asyncio.gather(*(self.send_message(member, '/prepare', prepare_msg)
                               for member in cluster_info['members']
                               if member['port'] not in [cluster_info['manager']['port'], self.node_info['port']]))
        return web.json_response({"status": "PREPARE broadcasted"})

    async def handle_prepare(self, request):
        data = await request.json()
        req_id = data.get("request_id")
        cluster_info = data.get("cluster_info")
        self.logger.info(f"[NODE {self.node_info['port']}] Received PREPARE for REQUEST {req_id} from {data.get('sender')}")
        status = self.get_status(req_id)
        status.add_message("PREPARE", data)
        if len(status.msgs.get("PREPARE", [])) >= (2 * self.f) and status.phase != "COMMIT":
            status.update_phase("COMMIT")
            commit_msg = {
                "request_id": req_id,
                "phase": "COMMIT",
                "data": data.get("data"),
                "timestamp": time.time(),
                "sender": self.node_info['port'],
                "cluster_info": cluster_info
            }
            self.logger.info(f"[NODE {self.node_info['port']}] Broadcasting COMMIT for REQUEST {req_id}")
            await asyncio.gather(*(self.send_message(member, '/commit', commit_msg)
                                   for member in cluster_info['members']
                                   if member['port'] not in [cluster_info['manager']['port'], self.node_info['port']]))
        return web.json_response({"status": "PREPARE processed"})

    async def handle_commit(self, request):
        data = await request.json()
        req_id = data.get("request_id")
        cluster_info = data.get("cluster_info")
        self.logger.info(f"[NODE {self.node_info['port']}] Received COMMIT for REQUEST {req_id} from {data.get('sender')}")
        status = self.get_status(req_id)
        status.add_message("COMMIT", data)
        if len(status.msgs.get("COMMIT", [])) >= (2 * self.f + 1) and status.phase != "PRE_REPLY":
            status.update_phase("PRE_REPLY")
            pre_reply_msg = {
                "request_id": req_id,
                "phase": "PRE_REPLY",
                "data": data.get("data"),
                "timestamp": time.time(),
                "sender": self.node_info['port'],
                "cluster_info": cluster_info
            }
            manager = cluster_info['manager']
            self.logger.info(f"[NODE {self.node_info['port']}] Sending PRE_REPLY for REQUEST {req_id} to Cluster Manager (Port {manager['port']})")
            await self.send_message(manager, '/pre_reply', pre_reply_msg)
        return web.json_response({"status": "COMMIT processed"})

    async def handle_pre_reply(self, request):
        data = await request.json()
        req_id = data.get("request_id")
        self.logger.info(f"[CLUSTER MANAGER {self.node_info['port']}] Received PRE_REPLY for REQUEST {req_id} from {data.get('sender')}")
        status = self.get_status(req_id)
        status.add_message("PRE_REPLY", data)
        if len(status.msgs.get("PRE_REPLY", [])) >= (self.f + 1) and not status.reply_received:
            status.reply_received = True
            reply_msg = {
                "request_id": req_id,
                "phase": "REPLY",
                "data": data.get("data"),
                "timestamp": time.time(),
                "sender": self.node_info['port']
            }
            client = next(d for d in self.config['drones'] if d['port'] == 20001)
            self.logger.info(f"[CLUSTER MANAGER {self.node_info['port']}] Sending REPLY for REQUEST {req_id} to Client (Port {client['port']})")
            await self.send_message(client, '/reply', reply_msg)
        return web.json_response({"status": "PRE_REPLY processed"})

    async def handle_reply(self, request):
        data = await request.json()
        self.logger.info(f"[NODE {self.node_info['port']}] Received REPLY (for logging): {data}")
        return web.json_response({"status": "REPLY received"})

    async def close(self):
        await self.session.close()

##########################################################################
# 헬퍼 함수: send_with_delay (공통)
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
    async with session.post(url, json=data) as resp:
        return await resp.text()

##########################################################################
# 설정 파일 파싱 함수
##########################################################################
def load_config(path):
    with open(path, 'r') as f:
        return yaml.safe_load(f)

##########################################################################
# 메인 함수: --index 인자에 따라 클라이언트(DCC)와 노드(드론) 역할 분리
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
    else:
        logger = setup_logging(f"LLAPBFT_Node_{args.index}", f"node_{args.index}.log")
        logger.info(f"[NODE] Starting node at {node['host']}:{node['port']} | index={args.index:3} | f={config.get('f')}, k={config.get('k')}")

    if role == "client":
        client = LLAPBFTClient(config, logger, bandwidth_file=args.bandwidth)
        app = web.Application()
        app.add_routes([
            web.post('/reply', client.handle_reply),
            web.post('/start-protocol', client.start_protocol)
        ])
        runner = web.AppRunner(app)
        await runner.setup()
        client_addr = client.client
        site = web.TCPSite(runner, host=client_addr['host'], port=client_addr['port'])
        await site.start()
        logger.info(f"[CLIENT] Server started at http://{client_addr['host']}:{client_addr['port']}")
        logger.info("Waiting for /start-protocol trigger (use curl to start consensus)...")
        while True:
            await asyncio.sleep(3600)
        await client.close()
        await runner.cleanup()
    else:
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
        node_addr = node_instance.node_info
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
