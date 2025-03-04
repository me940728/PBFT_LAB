#!/usr/bin/env python3
"""
pbft.py

PBFT 합의 시뮬레이션 코드 (지연시간 반영 버전)
- YAML 구성 파일에는 protocol: pbft, f: 4, k: 6, drones: 리스트가 있습니다.
- drones 리스트 중 port가 20001인 항목은 클라이언트 역할을 담당합니다.
- 나머지는 복제자(노드)로 동작합니다.
- 로그는 현재 디렉터리의 log/pbft 폴더에 생성되며, 기존 로그 파일에 이어서 기록됩니다.
- 클라이언트는 기본적으로 /start-protocol 엔드포인트에서 POST 요청을 받으면 합의 라운드를 시작합니다.
  (예: curl -X POST http://localhost:20001/start-protocol -H "Content-Type: application/json" -d '{"latitude": 36.6261519, "longitude": 127.4590123, "altitude": 0}')
- 모든 합의 라운드가 종료되면 클라이언트는 ★★★ 와 같은 특수문자를 포함하여 전체 소요 시간을 로그에 남깁니다.
- 전송 시, 각 노드간 거리에 따른 지연시간(simulate_delay)이 적용되며, 그 결과(거리, delay 등)는 수신측 로그 맨 끝에 출력됩니다.
"""

import asyncio, aiohttp, logging, time, argparse, os, yaml, json
from aiohttp import web
from common import calculate_distance, simulate_delay

##########################################################################
# 헬퍼 함수: 지연시간 적용 후 메시지 전송
##########################################################################
async def send_with_delay(session, source, target, url, data, bandwidth_data):
    if bandwidth_data:
        source_coords = (source.get('latitude', 0), source.get('longitude', 0), source.get('altitude', 0))
        target_coords = (target.get('latitude', 0), target.get('longitude', 0), target.get('altitude', 0))
        distance = calculate_distance(source_coords, target_coords)
        message_size_bits = len(json.dumps(data)) * 8
        delay = await simulate_delay(distance, message_size_bits, bandwidth_data)
        delay_logger = logging.getLogger("DelayLogger")
        delay_logger.info(
            f"[DELAY] 송신: Port{source['port']} → 수신: Port{target['port']} | "
            f"거리: {distance:.2f} m | 메시지 크기: {message_size_bits} bit | 지연: {delay:.4f} sec"
        )
        # 지연정보를 메시지에 첨부 (시뮬레이션용)
        data["simulated_delay"] = delay
        data["distance"] = distance
        data["message_size_bits"] = message_size_bits
    async with session.post(url, json=data) as resp:
        return await resp.text()

##########################################################################
# 로깅 설정: log/pbft 폴더에 append 모드로 로그 기록
##########################################################################
def setup_logging(name, log_file_name):
    log_dir = os.path.join(os.getcwd(), "log", "pbft")
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
# PBFT 합의 단계 상태 클래스
##########################################################################
class Status:
    # PBFT 단계: PRE-PREPARED → PREPARED → COMMITTED → REPLIED
    PREPREPARED = 'pre-prepared'
    PREPARED = 'prepared'
    COMMITTED = 'committed'
    REPLIED = 'replied'

    def __init__(self, f):
        self.f = f
        self.phase = None
        self.preprepare_msg = None
        self.prepare_msgs = []
        self.commit_msgs = []
        self.reply_sent = False

    def add_prepare(self, msg):
        self.prepare_msgs.append(msg)

    def add_commit(self, msg):
        self.commit_msgs.append(msg)

    def is_prepared(self):
        return len(self.prepare_msgs) >= (2 * self.f)

    def is_committed(self):
        return len(self.commit_msgs) >= (2 * self.f + 1)

##########################################################################
# PBFT Client 클래스 (port 20001인 항목)
##########################################################################
class PBFTClient:
    def __init__(self, config, logger, bandwidth_file=None):
        self.config = config
        self.logger = logger
        # drones 리스트 중 port 20001인 항목을 클라이언트로 사용
        self.client = next(d for d in config['drones'] if d['port'] == 20001)
        # 복제자 리스트: port가 20001이 아닌 항목들
        self.replicas = [d for d in config['drones'] if d['port'] != 20001]
        self.f = config.get('f', 1)
        self.session = aiohttp.ClientSession()
        self.bandwidth_file = bandwidth_file
        if self.bandwidth_file and os.path.exists(self.bandwidth_file):
            with open(self.bandwidth_file, 'r') as f:
                self.bandwidth_data = yaml.safe_load(f)
        else:
            self.bandwidth_data = None
        self.total_rounds = 1  # 합의 라운드 수
        self.reply_events = {}  # request_id별 asyncio.Event

    def get_leader(self):
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
        event = asyncio.Event()
        self.reply_events[req_id] = event
        await send_with_delay(self.session, self.client, leader, url, data, self.bandwidth_data)
        await event.wait()
        self.logger.info(f"[CLIENT] << REPLY received for REQUEST {req_id}")

    async def start_protocol(self, request):
        self.logger.info("★★★ /start-protocol 요청 수신: 합의 라운드를 시작합니다. ★★★")
        total_start_time = time.time()
        round_times = []
        for i in range(1, self.total_rounds + 1):
            round_start = time.time()
            await self.send_request()
            await asyncio.sleep(1)
            round_duration = time.time() - round_start
            round_times.append(round_duration)
            self.logger.info(f"[CLIENT] 라운드 {i} 완료: 수행 시간 = {round_duration:.4f} seconds")
        total_duration = time.time() - total_start_time
        self.logger.info(f"★★★ CONSENSUS COMPLETED: Total Time = {total_duration:.4f} seconds ★★★")
        return web.json_response({
            "status": "protocol started",
            "total_time": total_duration,
            "round_times": round_times
        })

    async def handle_reply(self, request):
        data = await request.json()
        req_id = data.get("request_id")
        self.logger.info(f"[CLIENT] REPLY: {data}")
        if req_id in self.reply_events:
            self.reply_events[req_id].set()
        return web.json_response({"status": "reply received"})

    async def close(self):
        await self.session.close()

##########################################################################
# PBFT Node 클래스 (복제자 역할; 클라이언트는 제외)
##########################################################################
class PBFTNode:
    def __init__(self, index, config, logger, bandwidth_file=None):
        self.index = index
        self.config = config
        self.logger = logger
        self.node_info = config['drones'][index]
        # 클라이언트(port 20001)를 제외한 복제자 목록
        self.replicas = [d for d in config['drones'] if d['port'] != 20001]
        self.f = config.get('f', 1)
        self.k = config.get('k', None)
        self.leader = self.get_leader()
        self.is_leader = (self.node_info['port'] == self.leader['port'])
        self.statuses = {}  # request_id별 합의 상태
        self.session = aiohttp.ClientSession()
        self.bandwidth_file = bandwidth_file
        if self.bandwidth_file and os.path.exists(self.bandwidth_file):
            with open(self.bandwidth_file, 'r') as f:
                self.bandwidth_data = yaml.safe_load(f)
        else:
            self.bandwidth_data = None

    def get_leader(self):
        sorted_replicas = sorted(self.replicas, key=lambda d: d['port'])
        return sorted_replicas[0]

    async def send_message(self, target, endpoint, data):
        url = f"http://{target['host']}:{target['port']}{endpoint}"
        try:
            await send_with_delay(self.session, self.node_info, target, url, data, self.bandwidth_data)
        except Exception as e:
            self.logger.error(f"[NODE {self.node_info['port']}] Error sending to {url}: {e}")

    async def broadcast(self, endpoint, message):
        tasks = [
            self.send_message(replica, endpoint, message)
            for replica in self.replicas if replica['port'] != self.node_info['port']
        ]
        await asyncio.gather(*tasks)

    async def handle_request(self, request):
        data = await request.json()
        req_id = data.get("request_id")
        self.logger.info(f"[NODE {self.node_info['port']}] Received REQUEST {req_id}")
        if not self.is_leader:
            redirect_url = f"http://{self.leader['host']}:{self.leader['port']}/request"
            self.logger.info(f"[NODE {self.node_info['port']}] Redirecting REQUEST {req_id} to Leader({self.leader['port']})")
            raise web.HTTPTemporaryRedirect(redirect_url)
        preprepare_msg = {
            "request_id": req_id,
            "data": data.get("data"),
            "timestamp": time.time(),
            "leader": self.node_info['port']
        }
        self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].phase = Status.PREPREPARED
        self.statuses[req_id].preprepare_msg = preprepare_msg
        self.logger.info(f"[LEADER {self.node_info['port']}] Broadcasting PRE-PREPARE for REQUEST {req_id}")
        await self.broadcast('/preprepare', preprepare_msg)
        return web.json_response({"status": "PRE-PREPARE broadcasted"})

    async def handle_preprepare(self, request):
        data = await request.json()
        req_id = data.get("request_id")
        self.logger.info(f"[NODE {self.node_info['port']}] Received PRE-PREPARE for REQUEST {req_id} from Leader({data.get('leader')})")
        if "simulated_delay" in data and "distance" in data:
            self.logger.info(f"[NODE {self.node_info['port']}] Delay Info: 거리: {data['distance']:.2f} m, 지연: {data['simulated_delay']:.4f} sec")
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].preprepare_msg = data
        prepare_msg = {
            "request_id": req_id,
            "data": data.get("data"),
            "timestamp": time.time(),
            "sender": self.node_info['port']
        }
        self.logger.info(f"[NODE {self.node_info['port']}] Broadcasting PREPARE for REQUEST {req_id}")
        await self.broadcast('/prepare', prepare_msg)
        return web.json_response({"status": "PREPARE broadcasted"})

    async def handle_prepare(self, request):
        data = await request.json()
        req_id = data.get("request_id")
        sender = data.get("sender")
        self.logger.info(f"[NODE {self.node_info['port']}] Received PREPARE for REQUEST {req_id} from {sender}")
        if "simulated_delay" in data and "distance" in data:
            self.logger.info(f"[NODE {self.node_info['port']}] Delay Info: 거리: {data['distance']:.2f} m, 지연: {data['simulated_delay']:.4f} sec")
        if req_id not in self.statuses:
            self.statuses[req_id] = Status(self.f)
        self.statuses[req_id].add_prepare(data)
        if self.statuses[req_id].is_prepared() and self.statuses[req_id].phase != Status.PREPARED:
            self.statuses[req_id].phase = Status.PREPARED
            commit_msg = {
                "request_id": req_id,
                "data": self.statuses[req_id].preprepare_msg.get("data"),
                "timestamp": time.time(),
                "sender": self.node_info['port']
            }
            self.logger.info(f"[NODE {self.node_info['port']}] Broadcasting COMMIT for REQUEST {req_id}")
            await self.broadcast('/commit', commit_msg)
        return web.json_response({"status": "PREPARE processed"})

    async def handle_commit(self, request):
        data = await request.json()
        req_id = data.get("request_id")
        sender = data.get("sender")
        self.logger.info(f"[NODE {self.node_info['port']}] Received COMMIT for REQUEST {req_id} from {sender}")
        if "simulated_delay" in data and "distance" in data:
            self.logger.info(f"[NODE {self.node_info['port']}] Delay Info: 거리: {data['distance']:.2f} m, 지연: {data['simulated_delay']:.4f} sec")
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

    async def handle_reply(self, request):
        data = await request.json()
        self.logger.info(f"[NODE {self.node_info['port']}] Received REPLY (for logging): {data}")
        return web.json_response({"status": "REPLY received"})

    async def close(self):
        await self.session.close()

##########################################################################
# 설정 파일 파싱 함수
##########################################################################
def load_config(path):
    with open(path, 'r') as f:
        return yaml.safe_load(f)

##########################################################################
# 메인 함수: --index 인자에 따라 역할 결정 (클라이언트: port 20001, 노드: 나머지)
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
        logger.info(
            f"[NODE] Starting node at {node['host']}:{node['port']} | index={args.index:3} | "
            f"f={config.get('f')}, k={config.get('k')} | Role: {role_str}"
        )
    
    if role == "client":
        client = PBFTClient(config, logger, bandwidth_file=args.bandwidth)
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
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Shutting down client.")
        await client.close()
        await runner.cleanup()
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