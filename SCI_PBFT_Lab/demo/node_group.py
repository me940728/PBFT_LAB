#! /usr/bin/env python3
# -*- coding: utf-8 -*-
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
import os
# from datetime import datetime
import hashlib

VIEW_SET_INTERVAL = 10

class View:
    
    def __init__(self, view_number, num_nodes):
        self._view_number = view_number # view 번호
        self._num_nodes = num_nodes #노드 수
        self._min_set_interval = VIEW_SET_INTERVAL  # view change 최소 간격
        self._last_set_time = time.time()   

    # To encode to json
    
    def get_view(self):
        return self._view_number

    # Recover from json data.
    
    def set_view(self, view):

        if time.time() - self._last_set_time < self._min_set_interval:
            return False
        self._last_set_time = time.time()
        self._view_number = view
        return True

class Status:
    PREPARE = 'prepare'
    COMMIT = 'commit'
    REPLY = "reply"

    
    def __init__(self, f):
        self.f = f  # Fault Node의 최대 허용 수
        self.request = 0    
        self.prepare_msgs = {}
        self.prepare_certificate = None  # proposal
        self.commit_msgs = {}
        self.commit_certificate = None  # proposal
        self.is_committed = False

    class Certificate:
        
        def __init__(self, view, proposal=0):
            self._view = view
            self._proposal = proposal

        
        def to_dict(self):
            return {
                'view': self._view.get_view(),
                'proposal': self._proposal
            }

        
        def dumps_from_dict(self, dictionary):
            self._view.set_view(dictionary['view'])
            self._proposal = dictionary['proposal']
        
        def get_proposal(self):
            return self._proposal

    class SequenceElement:
        
        def __init__(self, proposal):
            self.proposal = proposal
            self.from_nodes = set([])

    # 노드로부터 받은 PREPARE 또는 COMMIT 메시지를 prepare_msgs 또는 commit_msgs에 추가하는 메서드
    
    def _update_sequence(self, msg_type, view, proposal, from_node):
        hash_object = hashlib.sha256(
            json.dumps(proposal, sort_keys=True).encode())
        key = (view.get_view(), hash_object.digest())
        if msg_type == Status.PREPARE:
            if key not in self.prepare_msgs:
                self.prepare_msgs[key] = self.SequenceElement(proposal)
            self.prepare_msgs[key].from_nodes.add(from_node)
        elif msg_type == Status.COMMIT:
            if key not in self.commit_msgs:
                self.commit_msgs[key] = self.SequenceElement(proposal)
            self.commit_msgs[key].from_nodes.add(from_node)

    # PREPARE 또는 COMMIT 메시지가 다수결을 이루는지 확인하는 메서드(2f + 1)
    
    def _check_majority(self, msg_type):
        if msg_type == Status.PREPARE:
            if self.prepare_certificate:
                return True
            for key in self.prepare_msgs:
                if len(self.prepare_msgs[key].from_nodes) >= 2 * self.f + 1:
                    return True
            return False

        if msg_type == Status.COMMIT:
            if self.commit_certificate:
                return True
            for key in self.commit_msgs:
                if len(self.commit_msgs[key].from_nodes) >= 2 * self.f + 1:
                    return True
            return False

# 합의 알고리즘의 체크포인트 기능을 위한 클래스
class CheckPoint:
    RECEIVE_CKPT_VOTE = 'receive_ckpt_vote'
    
    def __init__(self, checkpoint_interval, nodes, f, node_index,
                 lose_rate=0, network_timeout=10):
        self._checkpoint_interval = checkpoint_interval
        self._nodes = nodes
        self._f = f
        self._node_index = node_index
        self._loss_rate = lose_rate
        self._log = logging.getLogger(__name__)

        self.next_slot = 0

        self.checkpoint = []

        self._received_votes_by_ckpt = {}
        self._session = None
        self._network_timeout = network_timeout

    #각 체크포인트에 대한 투표 노드 집합 및 포인트 정보
    class ReceiveVotes:
        
        def __init__(self, ckpt, next_slot):
            self.from_nodes = set([])
            self.checkpoint = ckpt
            self.next_slot = next_slot
    
    def get_commit_upperbound(self):
        return self.next_slot + 2 * self._checkpoint_interval

    
    def _hash_ckpt(self, ckpt):
        hash_object = hashlib.sha256(json.dumps(ckpt, sort_keys=True).encode())
        return hash_object.digest()

    # 다른 노드로부터 체크포인트 투표를 받는 메서드
    
    async def receive_vote(self, ckpt_vote):
        ckpt = json.loads(ckpt_vote['ckpt'])
        next_slot = ckpt_vote['next_slot']
        from_node = ckpt_vote['node_index']

        hash_ckpt = self._hash_ckpt(ckpt)
        if hash_ckpt not in self._received_votes_by_ckpt:
            self._received_votes_by_ckpt[hash_ckpt] = (
                CheckPoint.ReceiveVotes(ckpt, next_slot))
        status = self._received_votes_by_ckpt[hash_ckpt]
        status.from_nodes.add(from_node)
        for hash_ckpt in self._received_votes_by_ckpt:
            if (self._received_votes_by_ckpt[hash_ckpt].next_slot > self.next_slot and
                    len(self._received_votes_by_ckpt[hash_ckpt].from_nodes) >= 2 * self._f + 1):
                self.next_slot = self._received_votes_by_ckpt[hash_ckpt].next_slot
                self.checkpoint = self._received_votes_by_ckpt[hash_ckpt].checkpoint

    # 체크포인트 투표를 제안하는 메서드
    
    async def propose_vote(self, commit_decisions):
        proposed_checkpoint = self.checkpoint + commit_decisions
        await self._broadcast_checkpoint(proposed_checkpoint,
                                         'vote', CheckPoint.RECEIVE_CKPT_VOTE)
    
    # 지정된 노드에 POST 요청을 보내느 메소드

    async def _post(self, nodes, command, json_data):

        if not self._session:
            timeout = aiohttp.ClientTimeout(self._network_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        for i, node in enumerate(nodes):
            # if random() > self._loss_rate:
            try:
                _ = await self._session.post(
                    self.make_url(node, command), json=json_data)
            except Exception as e:
                # resp_list.append((i, e))
                self._log.error(e)
                pass

    @staticmethod
    
    def make_url(node, command):
        return "http://{}:{}/{}".format(node['host'], node['port'], command)

    
    async def _broadcast_checkpoint(self, ckpt, msg_type, command):
        json_data = {
            'node_index': self._node_index,
            'next_slot': self.next_slot + self._checkpoint_interval,
            'ckpt': json.dumps(ckpt),
            'type': msg_type
        }
        await self._post(self._nodes, command, json_data)

    
    def get_ckpt_info(self):
        json_data = {
            'next_slot': self.next_slot,
            'ckpt': json.dumps(self.checkpoint)
        }
        return json_data

    
    def update_checkpoint(self, json_data):
        if json_data['next_slot'] > self.next_slot:

            self.next_slot = json_data['next_slot']
            self.checkpoint = json.loads(json_data['ckpt'])

    
    async def receive_sync(sync_ckpt):
        if sync_ckpt['next_slot'] > self._next_slot:
            self.next_slot = sync_ckpt['next_slot']
            self.checkpoint = json.loads(sync_ckpt['ckpt'])

    
    async def garbage_collection(self):
        deletes = []
        for hash_ckpt in self._received_votes_by_ckpt:
            if self._received_votes_by_ckpt[hash_ckpt].next_slot <= next_slot:
                deletes.append(hash_ckpt)
        for hash_ckpt in deletes:
            del self._received_votes_by_ckpt[hash_ckpt]

class Block:
    
    def __init__(self, index, transactions, timestamp, previous_hash):
        self.index = index
        self.transactions = transactions
        self.timestamp = timestamp
        self.hash = ''
        self.previous_hash = previous_hash

    
    def compute_hash(self):
        """
        A function that return the hash of the block contents.
        """
        block_string = json.dumps(self.__dict__, sort_keys=True)
        return hashlib.sha256(block_string.encode()).hexdigest()

    
    def get_json(self):
        return json.dumps(self.__dict__, indent=4, sort_keys=True)

class Blockchain:
    
    def __init__(self):
        self.commit_counter = 0
        self.length = 0
        self.chain = []
        self.create_genesis_block()
    
    def create_genesis_block(self):
        genesis_block = Block(0, ["Genenesis Block"], 0, "0")
        genesis_block.hash = genesis_block.compute_hash()
        self.length += 1
        self.chain.append(genesis_block)

    # @property
    
    def last_block(self):
        return self.chain[-1]

    
    def last_block_hash(self):
        tail = self.chain[-1]

        return tail.hash

    
    def update_commit_counter(self):
        self.commit_counter += 1

    
    def add_block(self, block):
        previous_hash = self.last_block_hash()
        if previous_hash != block.previous_hash:
            raise Exception('block.previous_hash not equal to last_block_hash')
            return
        block.hash = block.compute_hash()
        self.length += 1
        self.chain.append(block)

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

    
    def __init__(self, index, conf, port, group, leaf_list, leader):
        # self._nodes = conf['nodes']
        self._nodes = group
        self._leaf_list = leaf_list
        self._node_cnt = len(self._nodes)
        self._index = index
        # Number of faults tolerant.
        self._f = (self._node_cnt - 1) // 3
        self.port = port
        # leader
        self.leader = leader
        self._view = View(0, self._node_cnt)
        self._next_propose_slot = 0

        self._blockchain = Blockchain()
        self.committed_to_blockchain = False

        self._loss_rate = 0  # conf['loss%'] / 100

        self._network_timeout = 9999  # conf['misc']['network_timeout']
        self._checkpoint_interval = conf['ckpt_interval']
        self._k = conf['k']
        self._ckpt = CheckPoint(self._checkpoint_interval, self._nodes,
                                self._f, self._index, self._loss_rate, self._network_timeout)
        # Commit
        self._last_commit_slot = -1
        self._collect_cnt = 0

        self._is_consensus_succeed = None
        self._parent = None
        self._follow_view = View(0, self._node_cnt)
        self._view_change_votes_by_view_number = {}

        self._status_by_slot = {}

        self._sync_interval = conf['sync_interval']

        self._session = None
        self._log = logging.getLogger(__name__)
        self._g_latency = conf["group_latency"]
        self._p_latency = conf['propagation_latency']

    @staticmethod
    
    def make_url(node, command):
        return "http://{}:{}/{}".format(node['host'], str(node['port']), command)

    # request 요청을 보내는 메소드
    
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
                    # resp_list.append((i, e))
                    self._log.error(e)
                    pass
        return resp_list

    # 요청을 받는 메소드
    
    async def _make_response(self, resp):
        if random() < self._loss_rate:
            await asyncio.sleep(self._network_timeout)
        return resp
    # post

    # POST 요청을 수행하는 메소드
    
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
                print("error: " + "  " + str(node) + "  port: " + str(self.port))
                print(exc_type, fname, exc_tb.tb_lineno)
                self._log.error(e)
                pass
        await asyncio.gather(*request_list)

    
    def _legal_slot(self, slot):

        if int(slot) < self._ckpt.next_slot or int(slot) >= self._ckpt.get_commit_upperbound():
            return False
        else:
            return True

    # Pre-prepare 동작을 수행하는 메소드
    
    async def preprepare(self, json_data):
        this_slot = str(self._next_propose_slot)
        self._next_propose_slot = int(this_slot) + 1

        if this_slot not in self._status_by_slot:
            self._status_by_slot[this_slot] = Status(self._f)
        self._status_by_slot[this_slot].request = json_data
        preprepare_msg = {
            'leader': self._index,
            'view': self._view.get_view(),
            'proposal': {
                this_slot: json_data
            },
            'type': 'preprepare'
        }
        await self._post(self._nodes, PBFTHandler.PREPARE, preprepare_msg)

    
    def _check_succeed(self):
        if self._collect_cnt >= len(self._leaf_list):
            self._collect_cnt = 0
            return True
        return False

    
    def dummy(self, request):
        return web.Response()

    # CPN으로 부터 요청을 받는 메소드
    
    async def get_request(self, request):
        if len(self._nodes) < self._k:
            return web.Response()
        # 리더 노드가 아닌 경우
        if not self.leader:
            if self.leader != None:
                raise web.HTTPTemporaryRedirect(self.make_url(
                    self._nodes[self.leader], PBFTHandler.REQUEST))
            else:
                raise web.HTTPServiceUnavailable()
        # 리더인 경우
        else:
            json_data = await request.json()    #request를 수신한 GPN
            self._is_consensus_succeed = asyncio.Event()
            if self._leaf_list: #하위 GPN이 있는 경우
                self._parent = json_data['client_url']  #합의 결과 반환 정보 저장해두기
                await asyncio.gather(self.propagation(json_data), self.preprepare(json_data))   # 두 작업을 병렬로 수행, 두 개에 대해 모두 대비
                await asyncio.wait_for(asyncio.gather(self._is_consensus_succeed.wait()), self._network_timeout)
            else:   #하위 GPN이 없는 경우
                self._parent = json_data['client_url']
                self._is_consensus_succeed = asyncio.Event()
                await self.preprepare(json_data)
                await asyncio.wait_for(asyncio.gather(self._is_consensus_succeed.wait()), self._network_timeout)
    
            await self.send_up()
            return web.Response()

    #상위로 데이터를 전달하는 메소드
    
    async def send_up(self):
        try:
            await asyncio.sleep(0.030)
            # await asyncio.sleep(0.1196475)  # 0822 testcode
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

    # Leaf list에 request 보내기
    
    async def propagation(self, reqeust):
        await self._post(self._leaf_list, PBFTHandler.REQUEST, reqeust)

    # Prepare 동작을 수행하는 메소드
    
    async def prepare(self, request):
        # print('prepare==')
        json_data = await request.json()
        self.committed_to_blockchain = False
        if json_data['view'] < self._follow_view.get_view():
            # when receive message with view < follow_view, do nothing
            return web.Response()
        for slot in json_data['proposal']:

            if not self._legal_slot(slot):
                continue

            if slot not in self._status_by_slot:
                self._status_by_slot[slot] = Status(self._f)

            prepare_msg = {
                'index': self._index,
                'view': json_data['view'],
                'proposal': {
                    slot: json_data['proposal'][slot]
                },
                'type': Status.PREPARE
            }
            await self._post(self._nodes, PBFTHandler.COMMIT, prepare_msg)
        return web.Response()

    # Commit 동작을 수행하는 메소드
    
    async def commit(self, request):
        # print('commit')
        json_data = await request.json()
        
        if json_data['view'] < self._follow_view.get_view():
            # when receive message with view < follow_view, do nothing
            return web.Response()
        for slot in json_data['proposal']:
            if not self._legal_slot(slot):
                continue

            if slot not in self._status_by_slot:
                self._status_by_slot[slot] = Status(self._f)
            status = self._status_by_slot[slot]

            view = View(json_data['view'], self._node_cnt)

            status._update_sequence(json_data['type'],
                                    view, json_data['proposal'][slot], json_data['index'])
            if status._check_majority(json_data['type']):
                status.prepare_certificate = Status.Certificate(view,
                                                                json_data['proposal'][slot])
                commit_msg = {
                    'index': self._index,
                    'view': json_data['view'],
                    'proposal': {
                        slot: json_data['proposal'][slot]
                    },
                    'type': Status.COMMIT
                }
                await self._post(self._nodes, PBFTHandler.REPLY, commit_msg)

        return web.Response()

    # Reply 단계를 수행하는 메소드
    
    async def reply(self, request):
        # print('reply')
        json_data = await request.json()

        if json_data['view'] < self._follow_view.get_view():
            # when receive message with view < follow_view, do nothing
            return web.Response()
        for slot in json_data['proposal']:
            if not self._legal_slot(slot):
                continue

            if slot not in self._status_by_slot:
                self._status_by_slot[slot] = Status(self._f)
            status = self._status_by_slot[slot]

            view = View(json_data['view'], self._node_cnt)

            status._update_sequence(json_data['type'],
                                    view, json_data['proposal'][slot], json_data['index'])

            if not status.commit_certificate and status._check_majority(json_data['type']):
                status.commit_certificate = Status.Certificate(view,
                                                               json_data['proposal'][slot])

                if self._last_commit_slot == int(slot) - 1 and not status.is_committed:

                    reply_msg = {
                        'index': self._index,
                        'view': json_data['view'],
                        'proposal': json_data['proposal'][slot],
                        'type': Status.REPLY,
                        'port': self.port
                    }
                    status.is_committed = True
                    self._last_commit_slot += 1

                    if (self._last_commit_slot + 1) % self._checkpoint_interval == 0:
                        await self._ckpt.propose_vote(self.get_commit_decisions())

                    if self.leader:
                        self._is_consensus_succeed.set()
        return web.Response()




def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


def arg_parse():
    # parse command line options
    parser = argparse.ArgumentParser(description='PBFT Node')
    parser.add_argument('-i', '--index', type=int, help='node index')
    parser.add_argument('-c', '--config', default='pbft.yaml',
                        type=argparse.FileType('r'), help='use configuration [%(default)s]')
    parser.add_argument('-lf', '--log_to_file', default=False, type=str2bool,
                        help='Whether to dump log messages to file, default = False')
    parser.add_argument('-cn', '--cluster_num', default='node_info.yaml', type=int,
                        help='use')

    args = parser.parse_args()
    return args


def conf_parse(conf_file) -> dict:
    conf = yaml.safe_load(conf_file)
    return conf


def aiohttp_server(pbft):
    def say_hello(request):
        return web.Response(text='Hello, world')

    app = web.Application()
    app.add_routes([
        web.post('/' + PBFTHandler.REQUEST, pbft.get_request),
        web.post('/' + PBFTHandler.PREPREPARE, pbft.preprepare),
        web.post('/' + PBFTHandler.PREPARE, pbft.prepare),
        web.post('/' + PBFTHandler.COMMIT, pbft.commit),
        web.post('/' + PBFTHandler.REPLY, pbft.reply),
        web.post('/' + PBFTHandler.RECEIVE_CKPT_VOTE, pbft.receive_ckpt_vote),
        web.post('/' + PBFTHandler.RECEIVE_SYNC, pbft.receive_sync),
        web.post('/' + PBFTHandler.VIEW_CHANGE_REQUEST,
                 pbft.get_view_change_request),
        web.post('/' + PBFTHandler.VIEW_CHANGE_VOTE,
                 pbft.receive_view_change_vote),
        web.get('/'+'blockchain', pbft.show_blockchain),
    ])
    runner = web.AppRunner(app)
    return runner


def run_server(runner, host, port):
    print(port)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, host, port)
    loop.run_until_complete(site.start())
    loop.run_forever()

# 인택스가 속한 그룹에 대한 정보를 반환하는 메소드

def get_group_info(nodes, k, index, r):
    #if문: 첫 그룹에 대한 노드 정보 반환
    if index == 0 or index < k:
        return nodes[0: k]
    else:
        group = 1  # len(nodes)//k [1. 첫 번째 if문이 아닌 경우에는 group이라는 변수를 1로 설정]
        '''
        for 루프를 통해 index가 속한 그룹을 찾기
        for 루프는 k*2부터 시작하여 k개의 간격으로 진행됨(index가 f보다 작아질 때까지 group을 1씩 증가)
        f가 index보다 크거나 같아지면 for 루프를 중단하고, 그 시점의 group이 index가 속한 그룹 번호가 됩니다.
        '''
        for f in range(k*2, len(nodes), k):
            if index < f:
                break
            group += 1

    # index가 속한 그룹의 마지막 노드가 전체 노드 리스트의 마지막 노드를 넘은 경우
    if (group*k)+k > len(nodes):
        # group*k부터 전체 노드 리스트의 끝까지를 반환
        return nodes[(group*k): len(nodes)]
    # 그 외의 경우 group*k부터 group*k+k까지의 노드 리스트를 반환
    return nodes[(group*k): (group*k)+k]


def main():
    # logging_config()
    log = logging.getLogger()
    log.debug('Start creating node!')  

    args = arg_parse()  # arg로 입력받은 데이터 설정
    conf = conf_parse(args.config)  # arg로 입력받은 yaml파일 정보 가져오기

    # log.error(conf)
    
    addr = conf['nodes'][args.index]  # - (args.k * args.group_number)]
    host = addr['host']
    port = addr['port']
    leader = False
    leaf_list = []
    if args.index == 0:
        #print("0")
        leader = True
    cnode = int(len(conf["nodes"])/int(args.cluster_num))   #node가 300개, cluster가 4개일때 cnode == 75, 120-4: 30

    index = 0
    # 클러스터 내 노드 ~전체 노드 리스트의 길이 + 1까지 클러스터 노드 수 만큼
    # ==> 클러스터 수 만큼 반복
    # ex. 120개 일때, 30,60, 90, 120
    for i in range(cnode, len(conf["nodes"])+1, cnode):
        if args.index < i:
            index = int(i // cnode) -1  # index == 노드가 속한 클러스터의 번호
            break
    
    # 현재 생성하는 노드가 마지막 클러스터의 여부에 따라
    # if(마지막 클러스터가 아닌 경우)-else(마지막 클러스터인 경우) 수행
    # clusert_node: 해당 클러스터 내의 노드 정보를 리스트(배열) 형태로 저장
    if args.index != args.cluster_num - 1:
        cluster_node = conf["nodes"][index * cnode : index * cnode + cnode]
    else:
        cluster_node = conf["nodes"][index * cnode :]

    # Input parameter: 
    #   nodes: list
    #   k: int
    #   args.index % cnode: int ==> 현재 노드가 클러스터 내에서 몇번째 노드인지
    #   r: int
    group = get_group_info(cluster_node, conf['k'], args.index % cnode, conf['r'])

    #현재 노드 정보 Key 중 leaf_list가 있는 경우 => GPN인 경우
    if 'leaf_list' in addr:
        leaf_list = addr['leaf_list']

    if leaf_list:
        #i: index, num: value(자식 GPN의 인덱스)
        for i, num in enumerate(leaf_list):
            leaf_list[i] = conf['nodes'][num]   # 자식 GPN 정보 리스트에 저장
    if 'leader' in addr:
        leader = True
    
    pbft = PBFTHandler(args.index, conf, port, group, leaf_list, leader)

    app = web.Application(client_max_size=1024**4)

    app.add_routes([
        web.post('/' + PBFTHandler.REQUEST, pbft.get_request),
        web.post('/' + PBFTHandler.PREPREPARE, pbft.preprepare),
        web.post('/' + PBFTHandler.PREPARE, pbft.prepare),
        web.post('/' + PBFTHandler.COMMIT, pbft.commit),
        web.post('/' + PBFTHandler.REPLY, pbft.reply)
    ])

    web.run_app(app, host=host, port=port,
                access_log=None)


if __name__ == "__main__":
    main()
