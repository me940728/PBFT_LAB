#! /usr/bin/env python3
import logging
import traceback
import argparse
from turtle import Turtle
import yaml
import time
from random import random, randint
from collections import Counter
import json
import sys
import os
import random
import asyncio
import aiohttp
from aiohttp import web
import threading

import hashlib
import math
VIEW_SET_INTERVAL = 10

# PBFT 합의 알고리즘을 위한 프로토콜을 처리하는 클래스
class PBFTHandler:
    REQUEST = 'request'
    PROPAGATION = 'propagation'
    PREPREPARE = 'preprepare'
    PREPARE = 'prepare'
    COMMIT = 'commit'
    REPLY = 'reply'

    NO_OP = 'NOP'

    RECEIVE_SYNC = 'receive_sync'
    RECEIVE_CKPT_VOTE = 'receive_ckpt_vote'

    VIEW_CHANGE_REQUEST = 'view_change_request'
    VIEW_CHANGE_VOTE = "view_change_vote"

    # 객체를 초기화하는 메소드로, 클러스터 노드 정보, 클러스터 노드 수, index 등을 설정
    def __init__(self, index, conf, port, cluster_node, args):
        self._nodes = cluster_node
        self._node_cnt = len(self._nodes)
        self._index = index
        self._f = (self._node_cnt - 1) // 3
        self.port = port
        self._next_propose_slot = 0
        self.conf = conf
        self.committed_to_blockchain = False
        self._cn = args.cluster_num
        self._k = conf["k"]
        self._group_cnt = len(self._nodes) / self._k
        self._loss_rate = 0  # conf['loss%'] / 100
        self._network_timeout = 9999  # conf['misc']['network_timeout']
        self._checkpoint_interval = conf['ckpt_interval']
        self._resend_interval = 99999
        self._last_commit_slot = -1
        self._collect_cnt = 0
        self._is_result_succeed = None
        self._is_consensus_succeed = None
        self._leader = 0
        self._cnt_reply =0
        self._parent = None
        self._view_change_votes_by_view_number = {}
        self._is_request_succeed = None
        self._status_by_slot = {}
        self._sync_interval = conf['sync_interval']
        self._session = None
        self._log = logging.getLogger(__name__)
        self._g_latency = conf["group_latency"]
        self._p_latency = conf['propagation_latency']
        # print("PBFT Handler initialized for node", self._index)


    #URL을 생성하는 정적 메소드
    @staticmethod
    def make_url(node, command):
        return "http://{}:{}/{}".format(node['host'], node['port'], command)

    # HTTP 요청을 생성하는 비동기 메소드
    async def _make_requests(self, nodes, command, json_data):
        resp_list = []
        for i, node in enumerate(nodes):
            if random() > self._loss_rate:
                if not self._session:
                    timeout = aiohttp.ClientTimeout(self._network_timeout)
                    self._session = aiohttp.ClientSession(timeout=timeout)
                try:
                    resp = await self._session.post(self.make_url(node, command), json=json_data)
                    resp_list.append((i, resp))

                except Exception as e:
               
                    self._log.error(e)
                    pass
        return resp_list

    # HTTP 요청을 처리하는 비동기 메소드
    async def _make_response(self, resp):
        '''
        Drop response by chance, via sleep for sometime.
        '''
        if random() < self._loss_rate:
            await asyncio.sleep(self._network_timeout)
        return resp

    # Node들에게 블록 메시지(JSON Format)를 POST 요청하는 메소드
    async def _post(self, nodes, command, json_data):
        if not self._session:
            timeout = aiohttp.ClientTimeout(self._network_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)

        await asyncio.sleep(0.03)

        request_list = []
        for i, node in enumerate(nodes):
            try:
                if command == PBFTHandler.REQUEST:
                    await asyncio.sleep(0.008)

                request_list.append(self._session.post(
                    self.make_url(node, command), json=json_data))
            except Exception as e:

                exc_type, exc_obj, exc_tb = sys.exc_info()
                fname = os.path.split(
                    exc_tb.tb_frame.f_code.co_filename)[1]
                print(exc_type, fname, exc_tb.tb_lineno)
                self._log.error(e)
        await asyncio.gather(*request_list)
    
    # CPN과 클라이언트 사이의 Reply GET 요청을 보내는 메소드
    async def get_reply(self, request):
        # print("Received a reply")
        json_data = await request.json()
        self._cnt_reply += 1
        if self._cnt_reply >= int(self._group_cnt/2):

            self._cnt_reply = 0
            self._is_request_succeed.set()

        return web.Response()

    # 클라이언트로 부터 전달 받은 Request 요청을 처리하는 메소드 
    async def get_request(self, request):
        # print("Received a request")

        if not self._session:
            timeout = aiohttp.ClientTimeout(self._resend_interval)
            self._session = aiohttp.ClientSession(timeout=timeout)

        json_data = await request.json()    # 블록메시지 가져오기
        self._is_request_succeed = asyncio.Event()
        self._parent = json_data['client_url']  #합의 결과를 반환할 Client 정보 가져오기
        json_data['client_url'] = "http://localhost:{}/reply".format(self.port)
        request_list = []

        # len(self._nodes) << 클러스터 당 노드 수
        # roofIndex를 통해 r보다 그룹 구성 노드 수가 적은 경우 대체
        roofIndex = int(self.conf["r"])
        if roofIndex > (len(self._nodes) / 4):
            roofIndex = math.ceil(len(self._nodes) / 4)

        # for i in range(0, int(self.conf["r"])):
        # 하위 그룹 수 만큼 반복, 하위 GPN에게 보낼 request_list를 생성
        for i in range(0, roofIndex):
            request_list.append(self._session.post(
                make_url(self._nodes[i * self._k], "request"), json=json_data))
            # print('GY test :: ', self._nodes[i * self._k])
            await asyncio.sleep(0.008)
        await asyncio.gather(*request_list) # GPN에 Broadcast
        await asyncio.wait_for(self._is_request_succeed.wait(), self._resend_interval)  #합의 결과 대기
        
        await self.send_up()    # Reply 수행 메소드 호출
        return web.Response()
        

    # 요청 결과를 상위 노드로 보내는 메소드
    async def send_up(self):
        try:
            # await asyncio.sleep(0.030)
            await asyncio.sleep(0.063)#inter Latency
            json_data = {'port': self.port}

            if self._session == None:
                timeout = aiohttp.ClientTimeout(
                    self._network_timeout)
                self._session = self._session = aiohttp.ClientSession(
                    timeout=timeout)
            await self._session.post(self._parent, json=json_data)

        except aiohttp.client_exceptions.ServerDisconnectedError as e:
            print(e)
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(
                exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            print("error port: {}    target : {}".format(self.port, self._parent))
            print("error : sens_up")



def arg_parse():
    # parse command line options
    parser = argparse.ArgumentParser(description='PBFT Node')
    parser.add_argument('-id', '--index', type=int, help='node index')
    parser.add_argument('-c', '--config', default='pbft.yaml',
                        type=argparse.FileType('r'), help='use configuration [%(default)s]')
    parser.add_argument('-cn', '--cluster_num', default='node_info.yaml', type=int,
                        help='use')
    args = parser.parse_args()
    return args


def conf_parse(conf_file) -> dict:
    conf = yaml.safe_load(conf_file)
    return conf

#HTTP 요청을 주고 받기 위한 URL 생성 함수
def make_url(node, command):
    return "http://{}:{}/{}".format(node['host'], node['port'], command)

def main(): 

    #python ./cpn.py -c $node_file -cn $c_int -id $i &
    #실행 파일 아규먼트 가져오기(c: yaml_file_path, cn: cluster_num, id: index)
    args = arg_parse()
    # print("Starting SEOUL PBFT node with index", args.index)
    # arg 중 config 파일 이름을 활용한 파일 정보 가져오기 
    conf = conf_parse(args.config)  
    host = 'localhost'
    port = 40000+args.index #CPN의 포트 번호 설정
    leader = False
    leaf_list = []

    # len(conf["nodes"]): yaml파일 전체 노드 수(GPN, NN), int(args.cluster_num): 클러스터 수
    cnode = int(len(conf["nodes"])/int(args.cluster_num))   
    index = int(args.index) #CPN의 ID

    #if: 해당 CPN이 마지막 클러스터의 CPN인 경우 수행 _ 마지막 클러스터에 대한 노드 정보 가져오기
    #else: CPN 0번 ~ cluster - 1번 까지인 경우 수행
    if args.index != args.cluster_num - 1:
        cluster_node = conf["nodes"][index * cnode : index * cnode + cnode]
    else:
        cluster_node = conf["nodes"][index * cnode :]

    #PBFT 핸들러 등록(CPN ID, yaml 파일 정보, CPN 포트 번호, 자신의 클러스터 내 노드 정보, args)
    pbft = PBFTHandler(args.index, conf, port, cluster_node, args)

    #클라이언트가 처리 가능한 요청 크기 설정
    app = web.Application(client_max_size=1024**4)

    #CPN의 동작 라우터 등록
    app.add_routes([
        web.post('/' + PBFTHandler.REQUEST, pbft.get_request),
        web.post('/reply', pbft.get_reply),
    ])

    
    web.run_app(app, host=host, port=port,
                access_log=None)


if __name__ == "__main__":
    main()
