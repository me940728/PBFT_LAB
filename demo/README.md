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
