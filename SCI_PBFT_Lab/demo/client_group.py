#! /usr/bin/env python3
import argparse
import asyncio
import threading
import time
from builtins import Exception
from random import random
import aiohttp
import yaml
from aiohttp import web
import os
from random import choice
from typing import Optional
#실행시 입력받은 arg를 가져오는 메소드
# -id   : client id
# -nm   : 합의 진행 횟수
# -c    : CPN config file path
# -cn   : cluster 갯수
# -nr   : 현재 수행중인 r값 _ 실험 로그용 args
# -tn   : 총 노드수 _ 실험 로그용 args

data: Optional[list] = None # 메시지 블록에 담을 데이터 초기화를 위한 전역변수 선언

def arg_parse():
    # parse command line options
    parser = argparse.ArgumentParser(description='PBFT Node')
    parser.add_argument('-id', '--client_id', type=int, help='client id')
    parser.add_argument('-nm', '--num_messages', default=0, type=int,
                        help='number of message want to send for this client')
    parser.add_argument('-c', '--config', default='node_info.yaml',
                        type=argparse.FileType('r'), help='use configuration [%(default)s]')
    parser.add_argument('-cn', '--cluster_num', default='node_info.yaml', type=int,
                        help='use')
    #GY test code
    parser.add_argument('-nr', '--numberOfR', default=0, type=int,
                        help='what is r')
    parser.add_argument('-tn', '--totalNode', default=0, type=int,
                        help='number of nodes')
    args = parser.parse_args()
    return args

# CPN 디렉토리의 yaml 파일 정보 로드
def conf_parse(conf_file) -> dict:
    conf = yaml.safe_load(conf_file)
    return conf

# HTTP 요청 및 처리를 위한 URL 생성 메소드
def make_url(node, command):
    return "http://{}:{}/{}".format(node['host'], node['port'], command)

# Client의 동작을 위한 클래스
class Client:
    REQUEST = "request"
    REPLY = "reply"
    VIEW_CHANGE_REQUEST = 'view_change_request'

    def __init__(self, conf, args):  # , log):
        self._nodes = conf['nodes']
        self._resend_interval = 99999  # conf['misc']['network_timeout']
        self._client_id = args.client_id
        self._num_messages = args.num_messages
        self._session = None
        self._address = conf['clients'][self._client_id]
        self._client_url = "http://{}:{}".format(self._address['host'],
                                                 self._address['port'])
        self._k = conf['k']
        self._r = conf['r']
        self._nr = args.numberOfR   #GY test add
        self._tn = args.totalNode
        self._cn = args.cluster_num
        self._retry_times = conf['retry_times_before_view_change']
        self._f = (len(self._nodes) - 1) // 3
        tmp = args.config.name.split('/')[0]
        self._yaml = tmp.split('.')[0]
        self._yaml = self._yaml.split('_')[0]
        self._is_request_succeed = None
        self._cnt_reply = 0
        self._group_number = len(self._nodes) // self._k
        self._c_latency = conf['client_latency']

    # Client가 view change를 수행하기 위한 HTTP 요청을 보내는 메소드
    async def request_view_change(self):
        json_data = {
            "action": "view change"
        }
        for i in range(len(self._nodes)):
            try:
                await self._session.post(make_url(
                    self._nodes[i], Client.VIEW_CHANGE_REQUEST), json=json_data)
            except:
                pass
            else:
                pass

    # CPN으로 부터 전달받은 각 클러스터의 합의 결과 메시지를 처리하는 메소드
    async def get_reply(self, request):
        json_data = await request.json()
        # print("[client_group, get_reply]final reply")
        self._cnt_reply += 1
        if self._cnt_reply >= self._cn:
            self._cnt_reply = 0
            self._is_request_succeed.set()
        return web.Response()

    # Client가 합의를 위한 블록 메시지를 CPN에게 보내기 위한 HTTP request를 보내는 메소드
    async def request(self):
        await asyncio.sleep(1)

        if not self._session:
            timeout = aiohttp.ClientTimeout(self._resend_interval)
            self._session = aiohttp.ClientSession(timeout=timeout)

        total_time = 0  #합의 진행 시간
        i = 1
        fail_msg = 0
        while i < self._num_messages:   # self._num_messages: 합의를 진행할 메시지 수, 현재 4회
            accumulate_failure = 0
            is_sent = False
            dest_ind = 0
            self._is_request_succeed = asyncio.Event()

            await asyncio.sleep(1)
            
            # 합의를 수행할 블록 메시지 JSON 포멧
            json_data = {
                'id': (self._client_id, i),
                'client_url': self._client_url + "/" + Client.REPLY,
                'timestamp': time.time(),
                'dummy_url': self._client_url + "/dummy",
                "data": data # 28 Line 전역변수
            }
            start = time.time() # 합의 시작 시간 측정
            while 1:
                try:
                    # latencyList = [0.02747, 0.129, 0.03662, 0.2855] #대한민국 - 일본, 미국 서부, 홍콩, 독일
                    # latencyList = [0.129, 0.2855]
                    latencyList = [0.063, 0.063]    #inter Latency
                    request_list = []
                    # print('nodes: ', self._nodes)
                    # CPN 수만큼 반복 -> request_list를 생성
                    for no in self._nodes:
                        request_list.append(self._session.post(make_url(no, Client.REQUEST), json=json_data))
                        # await asyncio.sleep(0.008)
                        await asyncio.sleep(float(choice(latencyList)))

                    await asyncio.gather(*request_list) #각 CPN에게 Broadcast 수행 - Pre-Request
                    await asyncio.wait_for(self._is_request_succeed.wait(), self._resend_interval)  # 특정 시간내로 작업을 완료하도록 제한
                except Exception as e:
                    print(e)
                else:
                    total_time += time.time() - start
                    print(str(i) + "--total time :", total_time)
                    i += 1
                    break

        # 합의 결과를 저장하기 위한 코드
        path = './results/'+str(self._k)+"/"+self._yaml+"_V_interLatency_063.txt"
        # 디렉터리가 존재하지 않으면 생성
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 파일을 append 모드로 열기
        f_file = open(path, 'a')
        # 결과 출력
        print(str(self._tn)+"\t"+str(self._nr) + "\t" + str(total_time), file=f_file)  # GY 출력문 수정
        # 파일 디스크립터 종료
        f_file.close()

        await self._session.close()

# Client에 대해 setup을 수행하는 메소드
def setup(args=None):

    if args == None:
        class Args:
            def __init__(self):
                self.client_id = 0
                self.num_messages = 0
                self.config = open('node_info.yaml', 'r')
        args = Args()

    conf = conf_parse(args.config)
    try:
        addr = conf['clients'][args.client_id]
    except Exception as e:
        import pdb
        pdb.set_trace()

    client = Client(conf, args)  # , log)
    return client

# 사용 안하는 메소드
def run_web(app, host, port):
    web.run_app(app, host=host, port=port)


def aiohttp_server(client):
    def say_hello(request):
        return web.Response(text='Hello, world')

    app = web.Application(client_max_size=1024**4)
    app.add_routes([web.post('/' + Client.REPLY, client.get_reply)])
    runner = web.AppRunner(app)
    return runner

# 이벤트 설정
def run_server(runner, host, port):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, host, port)
    loop.run_until_complete(site.start())
    loop.run_forever()

# 데이터를 동적으로 생성하는 함수
def generate_json_data() -> list:
    local_data = []
    count = 1  # 총 4889개의 데이터를 생성하기 위한 카운터
    
    # x는 0으로 고정, y는 1부터 14까지 순회
    while count <= 4889:  # 총 4889개의 데이터를 생성할 때까지 반복
        for y in range(1, 15):  # y는 1부터 14까지 반복
            if count > 4889:  # 4889개의 데이터까지만 생성
                break
            entry = [
                f"[0, {y}]",  # 첫 번째 요소, x는 고정, y만 변경
                f"send : {'a' * 27}",  # 예시로 'a'가 27번 반복된 문자열
                f"recieve : {'b' * 23}",  # 예시로 'b'가 23번 반복된 문자열
                f"data packet {y}",  # 'data packet'과 카운트 번호
                f"dummy : {'A' * 140}"  # 예시로 'A'가 140번 반복된 더미 데이터
            ]
            local_data.append(entry)
            count += 1  # 카운터를 증가
    
    return local_data

def main():
    args = arg_parse()
    conf = conf_parse(args.config)

    addr = conf['clients'][args.client_id]

    client = Client(conf, args)  # log)

    addr = client._address
    host = addr['host']
    port = addr['port']

    # Client 스레드를 생성 및 설정
    t = threading.Thread(target=run_server, args=(
        aiohttp_server(client), host, port))    
    t.start()

    # Client의 이벤트에 대한 루프 정보 등록 및 대기를 위한 코드
    loop = asyncio.get_event_loop() # 현재 이벤트 루프 정보 가져오기
    task = loop.create_task(client.request())   # 비동기 이벤트를 생성 및 이벤트 루프에 추가, HTTP 요청을 보내는 기능
    loop.run_until_complete(task)   # 이벤트가 종료될 때가지 루프를 실행

if __name__ == "__main__":
    data = generate_json_data() # 전역 변수 할당
    main()