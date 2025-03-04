#! /usr/bin/env python3
import logging
import argparse
import yaml
import time
from random import random
import json
import asyncio
import aiohttp
from aiohttp import web
import hashlib
import os

'''
    작업일 : '24.7.16 ~ 진행중
    작업자 : 최별규
    설명 : 합의를 수행하는 노드 객체
'''
VIEW_SET_INTERVAL = 10

LOG_DIR = 'log' # 로그 파일을 저장할 디렉터리
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
# PBFT 알고리즘에서 View를 관리하는 역할의 객체
class View:
    def __init__(self, view_number, num_nodes): 
        self._view_number = view_number              # 현재 뷰 번호
        self._num_nodes = num_nodes                  # 네트워크 존재하는 노드 수
        self._leader = view_number % num_nodes       # 현재 리더 노드의 번호 => 현재 뷰 번호를 노드수 만큼 나눈 나머지가 리더
        self._min_set_interval = VIEW_SET_INTERVAL   # 뷰 설정할 수 있는 최소 간격은 10(전역 변수로 관리되고 있음)
        self._last_set_time = time.time()            # 마지막으로 뷰가 설정된 시간
     # To encode to json
    def get_view(self):
        return self._view_number 
    # Recover from json data.
    def set_view(self, view):
        '''
        뷰 번호를 성공적으로 업데이트하면 True 실패하면 False
        '''
        if time.time() - self._last_set_time < self._min_set_interval:
            return False
        self._last_set_time = time.time()
        self._view_number = view
        self._leader = view % self._num_nodes
        return True

    def get_leader(self):
        return self._leader
# PBFT에서 각 슬롯의 상태를 기록하는 역할을 가진 객체
class Status:
    PREPARE = 'prepare'
    COMMIT = 'commit'
    REPLY = "reply"

    def __init__(self, f):
        self.f = f                      # 허용되는 Byzantine 노드의 수
        self.request = 0                # 현재 요청의 수
        self.prepare_msgs = {}          # 준비 메시지를 저장하는 딕셔너리
        self.prepare_certificate = None # 준비 증명서, proposal(제안)으로 사용
        self.commit_msgs = {}           # 커밋 메시지들을 저장하는 딕셔너리
        self.commit_certificate = None  # 커밋 증명서, proposal(제안)으로 사용
        self.is_committed = False       # 커밋 완료 여부를 나타내는 불리언 값
    # 제안서와 관련된 증명서 관리하는 역할
    class Certificate:
        def __init__(self, view, proposal=0): # 제안서는 초기 0 값임
            self._view = view
            self._proposal = proposal

        def to_dict(self):
            '''
            Convert the Certificate to dictionary
            증명서를 딕셔너리 형태로 변환하여 반환
            '''
            return {
                'view': self._view.get_view(),
                'proposal': self._proposal
            }
        # 딕셔너리 형태에서 증명서 정보를 업데이트
        def dumps_from_dict(self, dictionary):
            '''
            self.to_dict의 형식에서 뷰를 업데이트
            input:
                dictionay = {
                    'view': self._view.get_view(),
                    'proposal': self._proposal
                }
            '''
            self._view.set_view(dictionary['view'])
            self._proposal = dictionary['proposal']
        # 현재 제안서 반환
        def get_proposal(self):
            return self._proposal
    # 제안서와 제안서를 보낸온 노드들을 저장하는 역할
    class SequenceElement:
        def __init__(self, proposal):
            self.proposal = proposal
            self.from_nodes = set([]) # => proposal을 보낸 노드 집합 셋
    # 주어진 메시지 유형에 따라 상태를 업데이트
    def _update_sequence(self, msg_type, view, proposal, from_node):
        '''
        Update the record in the status by message type
        p2에 전달된 메시지 인자값에 따라 상태 기록을 업데이트 함
        input:
            msg_type: Status.PREPARE or Status.COMMIT
            view: 현재 뷰
            proposal: proposal in json_data
            from_node: The node send given the message.
        '''
        # BFT 노드로부터 다른 제안서를 받을 경우를 대비해 키에 hash(proposal)을 포함시켜야 함
        # json.dumps의 정렬 키를 사용하여 동일한 문자열을 얻도록 합니다. hashlib을 사용하여 
        # 매번 동일한 해시를 얻는다.
        hash_object = hashlib.md5(json.dumps(proposal, sort_keys=True).encode())
        key = (view.get_view(), hash_object.digest())
        if msg_type == Status.PREPARE:
            if key not in self.prepare_msgs:
                self.prepare_msgs[key] = self.SequenceElement(proposal)
            self.prepare_msgs[key].from_nodes.add(from_node)
        elif msg_type == Status.COMMIT:
            if key not in self.commit_msgs:
                self.commit_msgs[key] = self.SequenceElement(proposal)
            self.commit_msgs[key].from_nodes.add(from_node)
    # 동일한 뷰에서 2f + 1 이상 msg_type 메시지를 받았는지 확인
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

class CheckPoint:
    '''
    주어진 PBFTHandler(핸들러 == 메시지 처리 객체)에 대한 체크포인트의 모든 상태를 기록
    '''
    RECEIVE_CKPT_VOTE = 'receive_ckpt_vote'
    def __init__(self, checkpoint_interval, nodes, f, node_index, 
                 lose_rate=0, network_timeout=10):
        self._checkpoint_interval = checkpoint_interval                 # 체크포인트 간격
        self._nodes = nodes                                             # 노드 리스트
        self._f = f                                                     # 비잔틴 노드 수
        self._node_index = node_index                                   # 현재 노드의 인덱스
        self._loss_rate = lose_rate                                     # 메시지 손실율
        self._log = logging.getLogger(__name__)
        # 승인된 체크포인트의 다음 슬롯
        # 현재 체크포인트 슬롯이 99면 다음 슬롯은 100
        self.next_slot = 0
        # 전역적으로 승인된 체크포인트
        self.checkpoint = []
        # 체크포인트로부터 받은 투표를 해시값으로 기록
        self._received_votes_by_ckpt = {}
        self._session = None
        self._network_timeout = network_timeout

        self._log.info("Node %d: Create checkpoint", self._node_index)

    class ReceiveVotes:
        def __init__(self, ckpt, next_slot):
            self.from_nodes = set([])
            self.checkpoint = ckpt
            self.next_slot = next_slot
    # 현재 슬롯과 체크포인트 간격을 이용해서 커밋 가능한 최대 슬롯 번호 계산
    def get_commit_upperbound(self):
        '''
        Return the upperbound that could commit 
        (return upperbound = true upperbound + 1)
        '''
        return self.next_slot + 2 * self._checkpoint_interval

    def _hash_ckpt(self, ckpt):
        '''
        input: 
            ckpt: the checkpoint
        output:
            The hash of the input checkpoint in the format of 
            binary string.
        '''
        hash_object = hashlib.md5(json.dumps(ckpt, sort_keys=True).encode())
        return hash_object.digest()  

    async def receive_vote(self, ckpt_vote):
        '''
        Trigger when PBFTHandler receive checkpoint votes.
        First, we update the checkpoint status. Second, 
        update the checkpoint if more than 2f + 1 node 
        agree with the given checkpoint.
        input: 
            ckpt_vote = {
                'node_index': self._node_index
                'next_slot': self._next_slot + self._checkpoint_interval
                'ckpt': json.dumps(ckpt)
                'type': 'vote'
            }
        '''
        self._log.debug("Node %d: Receive checkpoint votes", self._node_index)
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
                self._log.info("Node %d: Update checkpoint by receiving votes", self._node_index)
                self.next_slot = self._received_votes_by_ckpt[hash_ckpt].next_slot
                self.checkpoint = self._received_votes_by_ckpt[hash_ckpt].checkpoint

    async def propose_vote(self, commit_decisions):
        '''
        When node the slots of committed message exceed self.next_slot 
        plus self._checkpoint_interval, propose new checkpoint and 
        broadcast to every node

        input: 
            commit_decisions: list of tuple: [((client_index, client_seq), data), ... ]

        output:
            next_slot for the new update and garbage collection of the Status object.
        '''
        proposed_checkpoint = self.checkpoint + commit_decisions
        await self._broadcast_checkpoint(proposed_checkpoint, 
                                         'vote', CheckPoint.RECEIVE_CKPT_VOTE)

    async def _post(self, nodes, command, json_data):
        '''
        Broadcast json_data to all node in nodes with given command.
        input:
            nodes: list of nodes
            command: action
            json_data: Data in json format.
        '''
        if not self._session:
            timeout = aiohttp.ClientTimeout(self._network_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        for i, node in enumerate(nodes):
            if random() > self._loss_rate:
                self._log.debug("Node %d: Sending request to node %d, command: %s", 
                                self._node_index, i, command)
                try:
                    await self._session.post(
                        self.make_url(node, command), json=json_data)
                except Exception as e:
                    self._log.error("Node %d: Failed to send request to node %d: %s", 
                                    self._node_index, i, str(e))
                    pass

    @staticmethod
    def make_url(node, command):
        '''
        input: 
            node: dictionary with key of host(url) and port
            command: action
        output:
            The url to send with given node and action.
        '''
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
        '''
        Get the checkpoint serialized information.Called 
        by synchronize function to get the checkpoint
        information.
        '''
        return {
            'next_slot': self.next_slot,
            'ckpt': json.dumps(self.checkpoint)
        }

    def update_checkpoint(self, json_data):
        '''
        Update the checkpoint when input checkpoint cover 
        more slots than current.
        input: 
            json_data = {
                'next_slot': self._next_slot
                'ckpt': json.dumps(ckpt)
            }     
        '''
        self._log.debug("Node %d: Update checkpoint: current next_slot: %d, update_slot: %d", 
                        self._node_index, self.next_slot, json_data['next_slot'])
        if json_data['next_slot'] > self.next_slot:
            self._log.info("Node %d: Checkpoint updated by synchronization.", self._node_index)
            self.next_slot = json_data['next_slot']
            self.checkpoint = json.loads(json_data['ckpt'])

    async def receive_sync(self, request):
        '''
        Trigger when recieve checkpoint synchronization messages.
        input: 
            sync_ckpt = {
                'node_index': self._node_index
                'next_slot': self._next_slot + self._checkpoint_interval
                'ckpt': json.dumps(ckpt)
                'type': 'sync'
            }
        '''
        self._log.debug("Node %d: Receive sync in checkpoint: current next_slot: %d", 
                        self._node_index, self.next_slot)

        sync_ckpt = await request.json()

        if sync_ckpt['next_slot'] > self.next_slot:
            self._log.info("Node %d: Sync update: next_slot: %d", 
                           self._node_index, sync_ckpt['next_slot'])
            self.next_slot = sync_ckpt['next_slot']
            self.checkpoint = json.loads(sync_ckpt['ckpt'])

    async def garbage_collection(self):
        '''
        Clean those ReceiveCKPT objects whose next_slot smaller
        than or equal to the current.
        '''
        deletes = []
        for hash_ckpt in self._received_votes_by_ckpt:
            if self._received_votes_by_ckpt[hash_ckpt].next_slot <= self.next_slot:
                deletes.append(hash_ckpt)
        for hash_ckpt in deletes:
            del self._received_votes_by_ckpt[hash_ckpt]
        self._log.info("Node %d: Garbage collection completed.", self._node_index)


class PBFTHandler:
    REQUEST = 'request'
    PREPREPARE = 'preprepare'
    PREPARE = 'prepare'
    COMMIT = 'commit'
    REPLY = 'reply'
    NO_OP = 'NOP'
    RECEIVE_SYNC = 'receive_sync'
    RECEIVE_CKPT_VOTE = 'receive_ckpt_vote'
    VIEW_CHANGE_REQUEST = 'view_change_request'
    VIEW_CHANGE_VOTE = "view_change_vote"

    def __init__(self, index, conf):
        self._nodes = conf['nodes']
        self._node_cnt = len(self._nodes)
        self._index = index
        self._f = (self._node_cnt - 1) // 3
        self._view = View(0, self._node_cnt)
        self._next_propose_slot = 0

        if self._index == 0:
            self._is_leader = True
        else:
            self._is_leader = False

        self._loss_rate = conf['loss%'] / 100
        self._network_timeout = conf['misc']['network_timeout']
        self._checkpoint_interval = conf['ckpt_interval']
        self._ckpt = CheckPoint(self._checkpoint_interval, self._nodes, 
                                self._f, self._index, self._loss_rate, self._network_timeout)
        self._last_commit_slot = -1
        self._leader = 0
        self._follow_view = View(0, self._node_cnt)
        self._status_by_slot = {}
        self._sync_interval = conf['sync_interval']
        self._session = None
        self._log = logging.getLogger(__name__) 
        self._log.info("Node %d: Initialized PBFT handler.", self._index)

    @staticmethod
    def make_url(node, command):
        return "http://{}:{}/{}".format(node['host'], node['port'], command)

    async def _post(self, nodes, command, json_data):
        if not self._session:
            timeout = aiohttp.ClientTimeout(self._network_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        for i, node in enumerate(nodes):
            if random() > self._loss_rate:
                self._log.debug("Node %d: Sending request to node %d, command: %s", 
                                self._index, i, command)
                try:
                    await self._session.post(self.make_url(node, command), json=json_data)
                except Exception as e:
                    self._log.error("Node %d: Failed to send request to node %d: %s", 
                                    self._index, i, str(e))
                    pass

    def _legal_slot(self, slot):
        if int(slot) < self._ckpt.next_slot or int(slot) >= self._ckpt.get_commit_upperbound():
            return False
        else:
            return True
    # [PREPREPARE Phase-3] : Group Primary Node로 부터 받은 요청메시지를 RN에게 브로드 캐스팅 하는 단계 
    async def preprepare(self, json_data):
        '''
        클라이언트로부터 받은 JSON 형식의 요청 데이터를 처리하고, 다른 복제 노드들에게 브로드캐스팅
        input:
            json_data: Json-transformed web request from client
                {
                    id: (client_id, client_seq),
                    client_url: "url string"
                    timestamp:"time"
                    data: "string"
                }

        '''
        this_slot = str(self._next_propose_slot)
        self._next_propose_slot = int(this_slot) + 1

        self._log.info("Node %d: Preprepare phase, proposing at slot: %d", 
                      self._index, int(this_slot))

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
    # [REQUEST Phase] : Phase-2으로 Client에서 Cluster Manage Node로 보낸 후 Group 에게 전달하는 단계
    async def get_request(self, request):
        '''
        [비동기 함수 핸들러]
        REQUEST 단계 : 요청 메시지가 클러스터에 브로드캐스팅 되었고, 각 요청 메시지는 클러스터내 그룹에게 전달이 되는 단계
        그룹의 리더 노드일 경우 요청을 처리하고 아닐경우 리더로 리다이렉트 함.
        '''
        self._log.info("Node %d: Received request from client", self._index)

        if not self._is_leader:      # 리더 노드가 아닌 경우
            if self._leader != None: # 리더 노드 정보를 확인 (None 이 아닌경우)
                self._log.warning("Node %d: Not leader, redirecting to leader node %d", 
                                  self._index, self._leader)
                # Client 에게 HTTP 307 리다이렉트 보냄
                raise web.HTTPTemporaryRedirect(self.make_url(
                    self._nodes[self._leader], PBFTHandler.REQUEST))
            else: # 리더 노드가 존재하지 않는 경우 HTTP 503을 보냄
                self._log.error("Node %d: No leader available, service unavailable", self._index)
                raise web.HTTPServiceUnavailable()
        else: # 리더 노드인 경우  
            json_data = await request.json() # 요청 데이터를 JSON으로 비동기적 파싱
            self._log.info("Node %d: Processing request as leader", self._index)
            await self.preprepare(json_data) # PRE-Prepare 단계를 수행함
            return web.Response()            # HTTP 200 OK 응답

    async def prepare(self, request):
        '''
        Once receive preprepare message from client, broadcast 
        prepare message to all replicas.

        input: 
            request: preprepare message from preprepare:
                preprepare_msg = {
                    'leader': self._index,
                    'view': self._view.get_view(),
                    'proposal': {
                        this_slot: json_data
                    }
                    'type': 'preprepare'
                }

        '''
        json_data = await request.json()

        if json_data['view'] < self._follow_view.get_view():
            return web.Response()

        self._log.info("Node %d: Received PRE-PREPARE message from Node %d", 
                      self._index, json_data['leader'])

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

    async def commit(self, request):
        json_data = await request.json()
        self._log.info("Node %d: Received PREPARE message from Node %d", 
                      self._index, json_data['index'])

        if json_data['view'] < self._follow_view.get_view():
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

    async def reply(self, request):
        json_data = await request.json()
        self._log.info("Node %d: Received COMMIT message from Node %d", 
                    self._index, json_data['index'])

        if json_data['view'] < self._follow_view.get_view():
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

                self._log.debug("Node %d: Added commit certificate to slot %d", 
                                self._index, int(slot))

                if self._last_commit_slot == int(slot) - 1 and not status.is_committed:
                    reply_msg = {
                        'index': self._index,
                        'view': json_data['view'],
                        'proposal': json_data['proposal'][slot],
                        'type': Status.REPLY
                    }
                    status.is_committed = True
                    self._last_commit_slot += 1

                    if (self._last_commit_slot + 1) % self._checkpoint_interval == 0:
                        await self._ckpt.propose_vote(self.get_commit_decisions())
                        self._log.info("Node %d: Proposing checkpoint with last slot: %d", 
                                    self._index, self._last_commit_slot)

                    await self._commit_action()
                    try:
                        await self._session.post(
                            json_data['proposal'][slot]['client_url'], json=reply_msg)
                    except Exception as e:
                        self._log.error("Node %d: Failed to send reply to client at %s. Error: %s", 
                                        self._index, json_data['proposal'][slot]['client_url'], str(e))
                    else:
                        self._log.info("Node %d: Successfully replied to client at %s", 
                                    self._index, json_data['proposal'][slot]['client_url'])
        return web.Response()


    def get_commit_decisions(self):
        commit_decisions = []
        for i in range(self._ckpt.next_slot, self._last_commit_slot + 1):
            status = self._status_by_slot[str(i)]
            proposal = status.commit_certificate.get_proposal()
            commit_decisions.append((proposal['id'], proposal['data']))

        return commit_decisions

    async def _commit_action(self):
        dump_file = os.path.join(LOG_DIR, f"{self._index}.dump")
        with open(dump_file, 'w') as f:
            dump_data = self._ckpt.checkpoint + self.get_commit_decisions()
            json.dump(dump_data, f)
        self._log.info("Node %d: Committed decisions to %s", self._index, dump_file)

    async def receive_ckpt_vote(self, request):
        self._log.info("Node %d: Received checkpoint vote.", self._index)
        json_data = await request.json()
        await self._ckpt.receive_vote(json_data)
        return web.Response()

    async def receive_sync(self, request):
        self._log.info("Node %d: Received sync request.", self._index)
        json_data = await request.json()
        self._ckpt.update_checkpoint(json_data['checkpoint'])
        self._last_commit_slot = max(self._last_commit_slot, self._ckpt.next_slot - 1)

        for slot in json_data['commit_certificates']:
            if int(slot) >= self._ckpt.get_commit_upperbound() or (
                    int(slot) < self._ckpt.next_slot):
                continue

            certificate = json_data['commit_certificates'][slot]
            if slot not in self._status_by_slot:
                self._status_by_slot[slot] = Status(self._f)
                commit_certificate = Status.Certificate(View(0, self._node_cnt))
                commit_certificate.dumps_from_dict(certificate)
                self._status_by_slot[slot].commit_certificate =  commit_certificate
            elif not self._status_by_slot[slot].commit_certificate:
                commit_certificate = Status.Certificate(View(0, self._node_cnt))
                commit_certificate.dumps_from_dict(certificate)
                self._status_by_slot[slot].commit_certificate =  commit_certificate

        while (str(self._last_commit_slot + 1) in self._status_by_slot and 
               self._status_by_slot[str(self._last_commit_slot + 1)].commit_certificate):
            self._last_commit_slot += 1

            if (self._last_commit_slot + 1) % self._checkpoint_interval == 0:
                await self._ckpt.propose_vote(self.get_commit_decisions())
                self._log.info("Node %d: During sync, proposed checkpoint with last slot: %d", 
                               self._index, self._last_commit_slot)

        await self._commit_action()

        return web.Response()
        
    async def synchronize(self):
        while 1:
            await asyncio.sleep(self._sync_interval)
            commit_certificates = {}
            for i in range(self._ckpt.next_slot, self._ckpt.get_commit_upperbound()):
                slot = str(i)
                if (slot in self._status_by_slot) and (self._status_by_slot[slot].commit_certificate):
                    status = self._status_by_slot[slot]
                    commit_certificates[slot] = status.commit_certificate.to_dict()
            json_data = {
                'checkpoint': self._ckpt.get_ckpt_info(),
                'commit_certificates':commit_certificates
            }
            await self._post(self._nodes, PBFTHandler.RECEIVE_SYNC, json_data)

    async def garbage_collection(self):
        await asyncio.sleep(self._sync_interval)
        delete_slots = []
        for slot in self._status_by_slot:
            if int(slot) < self._ckpt.next_slot:
                delete_slots.append(slot)
        for slot in delete_slots:
            del self._status_by_slot[slot]

        await self._ckpt.garbage_collection()

def logging_config(log_level=logging.INFO, log_file=None):
    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        return

    root_logger.setLevel(log_level)

    f = logging.Formatter("[%(asctime)s] [%(levelname)s] - %(message)s")

    h = logging.StreamHandler()
    h.setFormatter(f)
    h.setLevel(log_level)
    root_logger.addHandler(h)

    if log_file:
        log_path = os.path.join(LOG_DIR, log_file)
        from logging.handlers import TimedRotatingFileHandler
        h = TimedRotatingFileHandler(log_path, when='midnight', interval=1, backupCount=7)
        h.setFormatter(f)
        h.setLevel(log_level)
        root_logger.addHandler(h)


def arg_parse():
    parser = argparse.ArgumentParser(description='PBFT Node')
    parser.add_argument('-i', '--index', type=int, help='node index')
    parser.add_argument('-c', '--config', default='pbft.yaml', type=argparse.FileType('r'), help='use configuration [%(default)s]')
    parser.add_argument('-lf', '--log_to_file', default=False, type=bool, help='Whether to dump log messages to file, default = False')
    args = parser.parse_args()
    return args

def conf_parse(conf_file) -> dict:
    conf = yaml.safe_load(conf_file)
    return conf

def main():
    args = arg_parse() # 명령줄 인자값 파싱
    log_file = f'node_{args.index}.log' if args.log_to_file else None
    logging_config(log_file=log_file)
    
    log = logging.getLogger()
    log.info("Node %d: Starting PBFT node", args.index)
    
    conf = conf_parse(args.config) # -c => pbft.yaml 파일(default)
    log.debug(conf)
    
    addr = conf['nodes'][args.index]     # pbft.yaml 파일에서 nodes 가져옴 args.index는 run_node.sh 파일에서 0~3까지 순차적으로 입력된다.
    pbft = PBFTHandler(args.index, conf) # 인덱스는 외부 파일에서 순차적으로 생성함
     # asyncio.ensure_future() => 백그라운드에서 이벤트 루프 실행 하도록 예약(병렬로 실행)
    asyncio.ensure_future(pbft.synchronize())           # 시스템 상태 동기화
    asyncio.ensure_future(pbft.garbage_collection())    # 불필요 데이터 정리
    
    app = web.Application()
    app.add_routes([
        web.post('/' + PBFTHandler.REQUEST, pbft.get_request),
        web.post('/' + PBFTHandler.PREPREPARE, pbft.preprepare),
        web.post('/' + PBFTHandler.PREPARE, pbft.prepare),
        web.post('/' + PBFTHandler.COMMIT, pbft.commit),
        web.post('/' + PBFTHandler.REPLY, pbft.reply)
    ])
    
    web.run_app(app, host=addr['host'], port=addr['port'], access_log=None) # pbft.yaml.nodes[index]에서 host 가져옴, pbft.yaml.nodes[index]에서 port 가져옴

if __name__ == "__main__":
    main()
