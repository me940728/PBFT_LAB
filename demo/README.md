A simple PBFT protocol over HTTP, using python3 asyncio/aiohttp. This is just a proof of concept implementation.

## Configuration
A `pbft.yaml` config file is needed for the nodes to run. A sample is as follows:
```Yaml
nodes:
    - host: localhost
      port: 30000
    - host: localhost
      port: 30001
    - host: localhost
      port: 30002
    - host: localhost
      port: 30003

clients:
    - host: localhost
      port: 20001
    - host: localhost
      port: 20002

loss%: 0

ckpt_interval: 10

retry_times_before_view_change: 2

sync_interval: 5

misc:
    network_timeout: 5
```

## node.py 
- logging_config()
- arg_parse()
- conf_parse()
- main()
- View Class
    - init()
    - get_view()
    - set_view()
    - get_leader()
- Status Class
    - global Var : PREPARE, COMMIT, REPLY
    - init()
    - Certificate Class
        - init()
        - to_dict()
        - dumps_from_dict()
        - get_proposal()
    - SequenceElement Classs
        - init()
    - _update_sequence()
    - _check_majority()
- CheckPoint Class
    - init()
    - ReceiveVotes Class - init()
    - get_commit_upperbound()
    - _hash_ckpt()
    - async receive_vote()
    - async propose_vote()
    - async _post()
    - @staticmethod make_url()
    - async _broadcast_checkpoint()
    - get_ckpt_info()
    - update_checkpoint()
    - async receive_sync()
    - async garbage_collection()
- ViewChangeVotes Class
    - init()
    - receive_vote()
- PBFTHandler Class
    - global Var :
        - REQUEST, PREPREPARE, PREPARE, COMMIT, REPLY, NO_OP, RECEIVE_SYNC, RESEIVE_CKPT_VOTE, VIEW_CHANGE_REQUEST, VIEW_CHANGE_VOTE
    - init()
    - @staticmethod make_url()
    - async _make_requests()
    - async _make_response()
    - async _post()
    - _legal_slot()
    - async preprepare()
    - async get_request()
    - async prepare()
    - async commit()
    - async reply()
    - get_commit_decisions()
    - async _commit_action()
    - async receive_ckpt_vote()
    - async receive_sync()
    - async synchronize()
    - async get_prepare_certificates()
    - async post_view_change_vote()
    - async get_view_change_request()
    - async receive_view_change_vote()
    - async fill_bubbles()
    - async garbage_collection()

## Run the nodes
`for i in {0..3}; do python ./node.py -i $i -lf False  &; done`

## Send request to any one of the nodes
노드로 요청을 보내는 예시(e.g) : 
`curl -vLX POST --data '{ 'id': (0, 0), 'client_url': http://localhost:20001/reply, 'timestamp': time.time(), 'data': 'data_string' }' http://localhost:30000/request`
HTTP 요청을 verbose 모드, 리다이렉션 될 경우 따르며, -x POST 로 요청을 보낸다. --data는 {} 형식으로 http://localhost:30000/로 보낸다.
여기서 id는 (client_id, seq_id) 형태의 튜플이며, client_url은 get_reply 함수로 요청을 보내기 위한 URL이고, timestamp는 현재 시간이며, data는 문자열 형식의 데이터
* 먼저 서버들(client, node)들을 실행한 후 요청을 보낸다.

## Run the clients
`for i in {0...2}; do python client.py -id $i -nm 5 &; done`

## Environment
```
Python: 3.5.3
aiohttp: 3.4.4
yaml: 3.12
```
