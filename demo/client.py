import logging
import argparse
from logging.handlers import TimedRotatingFileHandler
import os
import yaml
import time
import json
import asyncio
import aiohttp
from aiohttp import web
from random import random
import hashlib

# View 클래스: 현재 뷰 상태를 관리하고 리더를 결정
class View:
    def __init__(self, view_number, num_nodes):
        self._view_number = view_number
        self._num_nodes = num_nodes
        self._leader = view_number % num_nodes

    # 현재 뷰 번호를 반환
    def get(self):
        return self._view_number 

    # 새로운 뷰를 설정하고 리더를 결정
    def set_view(self, view):
        self._view_number = view
        self._leader = view % self._num_nodes

# Status 클래스: 노드 응답 상태를 관리하고 합의 여부를 확인
class Status:
    def __init__(self, f):
        self.f = f  # 허용되는 최대 결함 수
        self.reply_msgs = {}  # 응답 메시지 저장

    # SequenceElement 클래스: 제안과 응답한 노드들을 저장
    class SequenceElement:
        def __init__(self, proposal):
            self.proposal = proposal
            self.from_nodes = set([])

    # 응답 메시지를 수신하고 상태를 업데이트
    def _update_sequence(self, view, proposal, from_node):
        hash_object = hashlib.md5(json.dumps(proposal, sort_keys=True).encode())
        key = (view.get(), hash_object.digest())
        if key not in self.reply_msgs:
            self.reply_msgs[key] = self.SequenceElement(proposal)
        self.reply_msgs[key].from_nodes.add(from_node)

    # 합의가 이루어졌는지 확인 (f + 1 이상의 응답)
    def _check_succeed(self):
        for key in self.reply_msgs:
            if len(self.reply_msgs[key].from_nodes) >= self.f + 1:
                return True
        return False

# 로깅 설정 함수: 로그를 콘솔 및 파일에 기록
def logging_config(log_level=logging.INFO, log_file=None):
    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        return

    root_logger.setLevel(log_level)

    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] - %(message)s")

    # 콘솔 핸들러 설정
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)
    root_logger.addHandler(console_handler)

    # 파일 핸들러 설정
    if log_file:
        log_dir = "log"  # 로그 파일이 저장될 디렉터리
        log_path = os.path.join(log_dir, log_file)
        try:
            file_handler = TimedRotatingFileHandler(log_path, when='midnight', interval=1, backupCount=7)
            file_handler.setFormatter(formatter)
            file_handler.setLevel(log_level)
            root_logger.addHandler(file_handler)
        except Exception as e:
            root_logger.error(f"Failed to set up file handler: {str(e)}")

# 명령줄 인자를 파싱하는 함수
def arg_parse():
    parser = argparse.ArgumentParser(description='PBFT Node')
    parser.add_argument('-id', '--client_id', type=int, help='client id')
    parser.add_argument('-nm', '--num_messages', default=10, type=int, help='number of messages to send for this client')
    parser.add_argument('-c', '--config', default='pbft.yaml', type=argparse.FileType('r'), help='use configuration [%(default)s]')
    args = parser.parse_args()
    return args

# YAML 설정 파일을 파싱하여 딕셔너리로 반환하는 함수
def conf_parse(conf_file) -> dict:
    conf = yaml.load(conf_file, Loader=yaml.SafeLoader)
    return conf

# 노드 URL을 생성하는 함수
def make_url(node, command):
    return "http://{}:{}/{}".format(node['host'], node['port'], command)

# 클라이언트 클래스: 요청을 보내고 응답을 처리
class Client:
    REQUEST = "request"
    REPLY = "reply"
    VIEW_CHANGE_REQUEST = 'view_change_request'

    def __init__(self, conf, args, log):
        self._nodes = conf['nodes']
        self._resend_interval = conf['misc']['network_timeout']
        self._client_id = args.client_id
        self._num_messages = args.num_messages
        self._session = None
        self._address = conf['clients'][self._client_id]
        self._client_url = "http://{}:{}".format(self._address['host'], self._address['port'])
        self._log = log

        self._retry_times = conf['retry_times_before_view_change']
        self._f = (len(self._nodes) - 1) // 3  # 허용되는 최대 결함 수

        self._is_request_succeed = None
        self._status = None

    # 뷰 변경 요청을 노드에 비동기로 전송
    async def request_view_change(self):
        json_data = {"action": "view change"}
        for i in range(len(self._nodes)):
            try:
                await self._session.post(make_url(self._nodes[i], Client.VIEW_CHANGE_REQUEST), json=json_data)
            except:
                self._log.info("---> %d failed to send view change message to node %d.", self._client_id, i)
            else:
                self._log.info("---> %d succeeded in sending view change message to node %d.", self._client_id, i)

    # 노드로부터 응답을 받아 처리
    async def get_reply(self, request):
        json_data = await request.json()
        if time.time() - json_data['proposal']['timestamp'] >= self._resend_interval:
            return web.Response()

        view = View(json_data['view'], len(self._nodes))
        self._status._update_sequence(view, json_data['proposal'], json_data['index'])

        if self._status._check_succeed():
            self._is_request_succeed.set()

        return web.Response()

    # 요청을 생성하고 노드에 전송
    async def request(self):
        if not self._session:
            timeout = aiohttp.ClientTimeout(self._resend_interval)
            self._session = aiohttp.ClientSession(timeout=timeout)
         
        for i in range(self._num_messages):
            accumulate_failure = 0
            is_sent = False
            dest_ind = 0
            self._is_request_succeed = asyncio.Event()
            await asyncio.sleep(random())
            json_data = {
                'id': (self._client_id, i),
                'client_url': self._client_url + "/" + Client.REPLY,
                'timestamp': time.time(),
                'data': str(i)
            }

            while 1:
                try:
                    self._status = Status(self._f)
                    await self._session.post(make_url(self._nodes[dest_ind], Client.REQUEST), json=json_data)
                    await asyncio.wait_for(self._is_request_succeed.wait(), self._resend_interval)
                except:
                    json_data['timestamp'] = time.time()
                    self._status = Status(self._f)
                    self._is_request_succeed.clear()
                    self._log.info("---> %d message %d sent fail.", self._client_id, i)

                    accumulate_failure += 1
                    if accumulate_failure == self._retry_times:
                        await self.request_view_change()
                        await asyncio.sleep(random())
                        accumulate_failure = 0
                        dest_ind = (dest_ind + 1) % len(self._nodes)
                else:
                    self._log.info("---> %d message %d sent successfully.", self._client_id, i)
                    is_sent = True
                if is_sent:
                    break
        await self._session.close()

# 메인 함수: 클라이언트를 실행하고 웹 서버를 시작
async def main():
    args = arg_parse()
    log_file = f'client_{args.client_id}.log'  # 클라이언트 ID를 기반으로 로그 파일명 지정
    logging_config(log_file=log_file)
    log = logging.getLogger()

    conf = conf_parse(args.config)
    log.debug(conf)

    addr = conf['clients'][args.client_id]
    log.info("begin")

    client = Client(conf, args, log)
    addr = client._address
    host = addr['host']
    port = addr['port']

    app = web.Application()
    app.add_routes([
        web.post('/' + Client.REPLY, client.get_reply),
    ])

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    await client.request()

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        log.info("Server is shutting down.")

    await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())