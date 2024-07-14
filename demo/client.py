#! conda 가상환경 
import logging
import argparse
import yaml
import time
import json
import asyncio
import aiohttp
from aiohttp import web
from random import random
import hashlib

# 분산시스템의 뷰, 리더를 선출(새롭게 다시 선출하기도 함)
class View:
    def __init__(self, view_number, num_nodes):
        self._view_number = view_number         # 생성자 0번째 인자 : 현재 뷰의 번호
        self._num_nodes = num_nodes             # 생성자 1번째 인자 : 시스템 내 노드 수
        self._leader = view_number % num_nodes  # 뷰 번호를 전체 노드로 나눈 나머지 ex : 2 % 3 = 2(몫은 0 나머지 2)
    # To encode to json
    def get(self):
        return self._view_number 
    # Recover from json data.
    def set_view(self, view):
        self._view_number = view
        self._leader = view % self._num_nodes # 새로운 리더 선출

# 상태 객체 => 제안(proposal)에 대한 응답 메시지를 기록하고 업데이트하는 역핳
class Status:
    # Status 객체의 인스턴스가 생성될 때 초기화하는 메서드(생성자)
    def __init__(self, f):   # 생성자의 0번째 인자는 무조건 self, self는 인스턴스 객체를 참조
        self.f = f           # 생성자의 1번째 파라미터의 인자값은 결함(Fault)의 수 이다.
        self.reply_msgs = {} # 빈 딕셔너리로 초기화
        
    # 합의 제안과 제안에 응답한 노드 저장
    class SequenceElement:
        def __init__(self, proposal):
            self.proposal = proposal  # 합의 제안 메시지 저장
            self.from_nodes = set([]) # 응답한 노드를 빈 집합으로 초기화

    def _update_sequence(self, view, proposal, from_node):
        '''
        응답 메시지를 수신할 때 상태의 기록을 업데이트
        input:
            view: View object of self._follow_view
            proposal: proposal in json_data => {"data", "proposal_data"} 형식으로 예상됨 
            from_node: 메시지를 보낸 노드
        '''

        # 제안이 BFT 노드로부터 다르게 올 경우를 대비하여 해시(proposal)를 키에 포함
        # 동일한 문자열을 얻기 위해 json.dumps에서 키를 정렬
        hash_object = hashlib.md5(json.dumps(proposal, sort_keys=True).encode())
        key = (view.get(), hash_object.digest()) # [1-1] => View객체에서 뷰 넘버를 가져와서 (key, hash) 형태 데이터 생성
        if key not in self.reply_msgs: # 회신 메시지 리스트에 해당하는 key가 없을 경우 => 중복된 응답 방지를 위한 검증
            self.reply_msgs[key] = self.SequenceElement(proposal) # SequenceElement 인스턴스를 생성하여 reply_msgs에 추가
        self.reply_msgs[key].from_nodes.add(from_node) # 해당 제안에 응답한 노드는 from_nodes 집합에 추가

    def _check_succeed(self):
        '''
        특정 유형의 메시지를 동일한 뷰에서 f + 1개 이상 수신했는지 확인합니다. * f는 결함 허용 임계값
        input:
            msg_type: self.PREPARE 또는 self.COMMIT
        '''
        
        for key in self.reply_msgs:
            if len(self.reply_msgs[key].from_nodes) >= self.f + 1:
                return True
        return False

def logging_config(log_level=logging.INFO, log_file=None):
    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        return

    root_logger.setLevel(log_level)

    f = logging.Formatter("[%(levelname)s]%(module)s->%(funcName)s: \t %(message)s \t --- %(asctime)s")

    h = logging.StreamHandler()
    h.setFormatter(f)
    h.setLevel(log_level)
    root_logger.addHandler(h)

    if log_file:
        from logging.handlers import TimedRotatingFileHandler
        h = TimedRotatingFileHandler(log_file, when='midnight', interval=1, backupCount=7)
        h.setFormatter(f)
        h.setLevel(log_level)
        root_logger.addHandler(h)

def arg_parse():
    # parse command line options
    parser = argparse.ArgumentParser(description='PBFT Node')
    parser.add_argument('-id', '--client_id', type=int, help='client id')
    parser.add_argument('-nm', '--num_messages', default=10, type=int, help='number of message want to send for this client')
    parser.add_argument('-c', '--config', default='pbft.yaml', type=argparse.FileType('r'), help='use configuration [%(default)s]')
    args = parser.parse_args()
    return args

def conf_parse(conf_file) -> dict:
    '''
    Sample config:

    nodes:
        - host: 
          port:
        - host:
          port:

    loss%:

    skip:

    heartbeat:
        ttl:
        interval:

    election_slice: 10

    sync_interval: 10

    misc:
        network_timeout: 10
    '''
    conf = yaml.load(conf_file)
    return conf

def make_url(node, command):
    return "http://{}:{}/{}".format(node['host'], node['port'], command)

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
        self._client_url = "http://{}:{}".format(self._address['host'], 
            self._address['port'])
        self._log = log

        self._retry_times = conf['retry_times_before_view_change']
        # Number of faults tolerant.
        self._f = (len(self._nodes) - 1) // 3

        # Event for sending next request
        self._is_request_succeed = None
        # To record the status of current request
        self._status = None

    async def request_view_change(self):
        json_data = {
            "action" : "view change"
        }
        for i in range(len(self._nodes)):
            try:
                await self._session.post(make_url(
                    self._nodes[i], Client.VIEW_CHANGE_REQUEST), json=json_data)
            except:
                self._log.info("---> %d failed to send view change message to node %d.", 
                    self._client_id, i)
            else:
                self._log.info("---> %d succeed in sending view change message to node %d.", 
                    self._client_id, i)



    async def get_reply(self, request):
        '''
        Count the number of valid reply messages and decide whether request succeed:
            1. Process the request only if timestamp is still valid(not stall)
            2. Count the number of reply message within same view, 
               if above f + 1, means success.
        input:
            request:
                reply_msg = {
                    'index': self._index,
                    'view': json_data['view'],
                    'proposal': json_data['proposal'][slot],
                    'type': Status.REPLY
                }
        output:
            Web.Response
        '''
        json_data = await request.json()
        if time.time() - json_data['proposal']['timestamp'] >= self._resend_interval:
            return web.Response()

        view = View(json_data['view'], len(self._nodes))
        self._status._update_sequence(view, json_data['proposal'], json_data['index'])

        if self._status._check_succeed():
            # self._log.info("Get reply from %d", json_data['index'])
            self._is_request_succeed.set()

        return web.Response()


    async def request(self):
        if not self._session:
            timeout = aiohttp.ClientTimeout(self._resend_interval)
            self._session = aiohttp.ClientSession(timeout = timeout)
         
        for i in range(self._num_messages):
            
            accumulate_failure = 0
            is_sent = False
            dest_ind = 0
            self._is_request_succeed = asyncio.Event()
            # Every time succeed in sending message, wait for 0 - 1 second.
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
                        # Sleep 0 - 1 second for view change
                        await asyncio.sleep(random())
                        accumulate_failure = 0
                        dest_ind = (dest_ind + 1) % len(self._nodes)
                else:
                    self._log.info("---> %d message %d sent successfully.", self._client_id, i)
                    is_sent = True
                if is_sent:
                    break
        await self._session.close()
    

def main():
    logging_config()
    log = logging.getLogger()
    args = arg_parse()
    conf = conf_parse(args.config)
    log.debug(conf)

    addr = conf['clients'][args.client_id]
    log.info("begin")
    

    client = Client(conf, args, log)

    addr = client._address
    host = addr['host']
    port = addr['port']


    asyncio.ensure_future(client.request())

    app = web.Application()
    app.add_routes([
        web.post('/' + Client.REPLY, client.get_reply),
    ])

    web.run_app(app, host=host, port=port, access_log=None)


    
    # loop = asyncio.get_event_loop()
    # loop.run_until_complete(client.request())

if __name__ == "__main__":
    main()

            
    
