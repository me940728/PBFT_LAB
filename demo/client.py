# conda 가상환경 
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
'''
    작업일 : '24.7.14 ~ 진행중
    작업자 : 최별규
    설명 : 합의 메시지를 보내는 클라이언트 객체
'''
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
        특정 유형의 메시지를 동일한 뷰에서 f + 1개 이상 수신했는지 확인 => * f는 결함 허용 임계값
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
        
# 커맨드 라인에 전달되는 인자 파싱하는 함수
def arg_parse():
    # parse command line options
    parser = argparse.ArgumentParser(description='PBFT Node') # ArgumentParser 객체 생성, description은 프로그램의 간단한 설명임
    parser.add_argument('-id', '--client_id', type=int, help='client id') # -id 또는 --client_id 옵션을 사용하여 클라이언트 ID 지정, 인자는 int형임, help는 인자 설명
    parser.add_argument('-nm', '--num_messages', default=10, type=int, help='number of message want to send for this client') # 클라이언트가 보내고자 하는 메시지 수 지정(기본 10개)
    parser.add_argument('-c', '--config', default='pbft.yaml', type=argparse.FileType('r'), help='use configuration [%(default)s]') # 설정 파일 지정, 기본 PBFY.yaml, 읽기 모드로 염
    args = parser.parse_args() # parse_args() 함수로 커맨드 라인 인자를 파싱하고 결과를 args로 저장
    return args # 저장된 args 값 리턴

# yaml 모듈을 활용한 설정 파일을 딕셔너리 형태로 반환하는 함수
def conf_parse(conf_file) -> dict:
    '''
    Sample config:

    nodes:
        - host: 
          port:
        - host:
          port:

    loss%: -> 손실율

    skip:

    heartbeat:
        ttl:
        interval:

    election_slice: 10

    sync_interval: 10 -> 동기화 간격

    misc: -> 기타 설정
        network_timeout: 10
    '''
    conf = yaml.safe_load(conf_file) # ['24.7.14.CHOI]코드 수정(보안상 이슈) yaml.load -> yaml.safe_load
    return conf

def make_url(node, command):
    return "http://{}:{}/{}".format(node['host'], node['port'], command) # {}에 format 파라미터와 대응하여 값이 들어감 host : 192.168.1.1, post : 8080, command : start.do

# 요청을 보내고, 응답을 받으면서 뷰 변경을 처리하는 클라이언트 객체(일정 시간 응답을 받지 못하면 뷰 변경)
class Client:
    #   [클라이언트가 처리할 수 있는 메시지 유형 정의한 클래스 Level 상수]
    REQUEST = "request" # 요청
    REPLY = "reply"     # 회신
    VIEW_CHANGE_REQUEST = 'view_change_request' # 뷰 변경 요청
    #=======================================================
    # 클라이언트 객체 초기화 메서드
    def __init__(self, conf, args, log): # p1 : yaml 설정 파일, p2 : 명령줄 인자, p3 : log
        self._nodes = conf['nodes'] # 노드들
        self._resend_interval = conf['misc']['network_timeout'] # 재전송 간격
        self._client_id = args.client_id # 클라이언트 아이디
        self._num_messages = args.num_messages # 메시지 수
        self._session = None # 세션 정보는 None
        self._address = conf['clients'][self._client_id] #
        self._client_url = "http://{}:{}".format(self._address['host'],self._address['port'])
        self._log = log

        self._retry_times = conf['retry_times_before_view_change'] # 재시도 횟수
        # Number of faults tolerant.
        self._f = (len(self._nodes) - 1) // 3 # 장애 허용 수

        # Event for sending next request
        self._is_request_succeed = None
        # To record the status of current request
        self._status = None

    # 뷰 변경을 모든 노드에게 비동기로 보내는 함수
    async def request_view_change(self):
        json_data = {
            "action" : "view change"
        }
        for i in range(len(self._nodes)): # 노드 수 만큼 Loop 돌려서 뷰 변경 요청 메시지를 보냄
            try:
                await self._session.post(make_url( # 133line make_url 함수 call
                    self._nodes[i], Client.VIEW_CHANGE_REQUEST), json=json_data)
            except: # 실패 로그
                self._log.info("---> %d failed to send view change message to node %d.", self._client_id, i)
            else:   # 성공 로그
                self._log.info("---> %d succeed in sending view change message to node %d.", self._client_id, i)

    # 클라이언트가 노드로부터 회신 받는 함수
    async def get_reply(self, request):
        '''
        유효한 응답 메시지의 수를 세고 요청이 성공했는지 결정:
            1. 타임스탬프가 아직 유효한 경우에만 요청을 처리(타임스탬프가 오래되지 않음).
            2. 동일한 뷰에서 f + 1 이상의 응답 메시지를 세면, 성공을 의미.
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
        json_data = await request.json() # JSON 데이터 파싱
        # 타임스탬프 유효성 검사
        if time.time() - json_data['proposal']['timestamp'] >= self._resend_interval: # 타임스탬프가 아직 유효한 경우에만 요청 처리
            return web.Response() # 만약 유효하지 않으면 빈 객체 반환
        # 뷰 객체 생성 및 상태 업데이트
        view = View(json_data['view'], len(self._nodes)) # ?
        self._status._update_sequence(view, json_data['proposal'], json_data['index']) #
        # 동일한 뷰에서 f + 1 이상의 응답 메시지를 수신하면 요청 성공을 반환
        if self._status._check_succeed():
            # self._log.info("Get reply from %d", json_data['index'])
            self._is_request_succeed.set()

        return web.Response()

    # 클라이언트가 노드로 합의 메시지를 요청하는 함수
    async def request(self):
        # 세션이 없으묜 새 세션을 형성
        if not self._session:
            timeout = aiohttp.ClientTimeout(self._resend_interval)
            self._session = aiohttp.ClientSession(timeout = timeout)
         
        for i in range(self._num_messages): # 메시지 수 만큼 Loop
            
            accumulate_failure = 0
            is_sent = False
            dest_ind = 0
            self._is_request_succeed = asyncio.Event()
            # 성공적으로 메시지를 보낼 때마다 0 - 1초 대기
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
# Client 객체를 실행하는 메인 함수    
def main():
    logging_config()
    log = logging.getLogger()
    args = arg_parse()
    conf = conf_parse(args.config)
    log.debug(conf)

    addr = conf['clients'][args.client_id] # 클라이언트의 주소만 가져옴
    log.info("begin")
    

    client = Client(conf, args, log)

    addr = client._address
    host = addr['host']
    port = addr['port']


    asyncio.ensure_future(client.request()) # 비동기 작업 수행하도록 예약 (ensure_future는 즉시 실행되지 않고 이벤트 루프에 작업 예약을 함)

    app = web.Application() # 앱 인스턴스 생성
    app.add_routes([
        web.post('/' + Client.REPLY, client.get_reply), # Client.REPLY 경로에 POST 작업을 수행할 이벤트 핸들러 추가 / 요청 들어오면 client.get_reply 실행
    ])

    web.run_app(app, host=host, port=port, access_log=None) # 웹 앱 실행(이벤트 루프 시작)


    
    # loop = asyncio.get_event_loop()
    # loop.run_until_complete(client.request())

if __name__ == "__main__":
    main()

            
    
