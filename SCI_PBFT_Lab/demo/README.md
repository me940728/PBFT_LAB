# BLOCKCHAIN_SCI PBFT


## Conda env setup

```yaml
- pip:
	- joblib==1.2.0
        - mpi4py==3.1.3
        - numpy==1.21.6
        - scikit-learn==1.0.2
        - scipy==1.7.3
        - threadpoolctl==3.1.0
```


## How to run

```Shell
./start.sh
```

./start.sh를 실행하면 노드가 120, 160, 200, 240, 280, 300개인 경우에 대해 테스트를 수행한다.

각 노드 갯수에 대하여 클러스터의 갯수와 최대 자식 그룹의 갯수를 조정해가면서 실행된다.

## 구성 파일
### 1. node_group.py
#### GPN, NN의 동작 코드
<!-- arg_parse: 입력받은 arg를 처리하는 메소드 -->
##### Arguments
    * -i, —index
        - index, 노드의 id
    * -c, —config
        - config파일, 노드에 대한 설정 파일(yaml파일)
    * -lf, —log_to_file
        - log를 남길지 여부를 결정하는 arg
        - Default: True
    * -cn, —cluster_num
        - cluster_num, 클러스터의 숫자를 입력하는 arg

### 2. cpn.py
#### CPN의 동작 코드
##### Arguments
    * -id, —index
        : index, CPN 노드의 ID 값(CPN 포트: baseport + id)
    * -c, —config
        : config파일, 노드에 대한 설정 파일(yaml파일)
    * -cn, —cluster_num
        : cluster_num, 클러스터의 숫자를 입력하는 arg
### 3. client_group.py
#### Client에 대한 동작 코드
##### Arguments
    * -id, —client_id
        : Client의 ID 값
    * -nm, --num_messages
        : 합의 진행 횟수, nm -1회
    * -c, —config
        : config파일, CPN 노드에 대한 설정 파일(yaml파일)
    * -cn, —cluster_num
        : cluster_num, 클러스터의 숫자를 입력하는 arg
    * -nr, --numberOfR
        : 결과 저장용, r 
    * -tn, --totalNode
        : 결과 저장용, 총 노드 수 
---
총 노드 수: 120개
클러스터 수: 4개
r: 4, 최대 자식 그룹의 수 
k: 4, 그룹 내 최대 노드의 수
<img src = "https://github.com/ISSR-CBNU/SCI_PBFT/blob/main/img/node_info_4r_120.jpg" width = "auto" height="auto"/>

---

### 실험 환경

<img src = "https://github.com/ISSR-CBNU/SCI_PBFT/blob/main/img/ExperimentComputingSpecifications.png" width = "auto" height="auto"/>


### Parameter

<!-- - node_info_{r}r_{totalNodeCount}.yaml

```yaml
loss%: 0

ckpt_interval: 10

retry_times_before_view_change: 1

sync_interval: 10

misc:
    network_timeout: 20

k : 4 # 하나의 그룹 내의 최대 노드 갯수

r : 2 # 하위로 가질 수 있는 최대 그룹 갯수

client_latency : 0.010
propagation_latency : 0.001
group_latency : 0.001
```

- start.sh

```bash
k="4"
node="400"
#r="2"
delay=1000
iter_cnt=7
cluster="4"
``` -->