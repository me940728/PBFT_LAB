import asyncio
import logging

# 전역 상태 변수 (예시)
reply_map = {}         # 각 라운드별로 받은 reply들을 저장
start_times = {}       # 각 라운드 시작 시각을 저장
current_seq = 0        # 현재 합의 시퀀스 번호 (라운드 번호)
consensus_done = set() # 합의 완료된 노드(또는 메시지)를 저장하는 집합

def clear_old_rounds():
    logging.info("clear_old_rounds 시작 전: reply_map keys: %s, start_times keys: %s",
                 list(reply_map.keys()), list(start_times.keys()))
    reply_map.clear()
    start_times.clear()
    logging.info("clear_old_rounds 완료 후: reply_map keys: %s, start_times keys: %s",
                 list(reply_map.keys()), list(start_times.keys()))

def clear_round_state():
    global current_seq, consensus_done
    logging.info("합의 완료 후, 상태 초기화: 이전 seq=%d, consensus_done: %s", current_seq, consensus_done)
    # 상태 초기화
    reply_map.clear()
    start_times.clear()
    consensus_done.clear()
    # 다음 라운드를 위해 seq 번호 증가
    current_seq += 1
    logging.info("새로운 합의 라운드 시작: 새로운 seq=%d", current_seq)

def handle_reply(message):
    """
    수신한 reply 메시지를 처리합니다.
    메시지의 'seq' 값이 현재 합의 라운드(current_seq)와 일치하는지 확인합니다.
    """
    msg_seq = message.get("seq")
    logging.info("[PBFT] handle_reply: 수신한 메시지 seq: %s, 현재 seq: %d, consensus_done: %s",
                 msg_seq, current_seq, consensus_done)
    if msg_seq != current_seq:
        logging.warning("수신된 seq(%s)가 현재 seq(%d)와 다릅니다.", msg_seq, current_seq)
        # 필요에 따라 무시하거나 오류 처리할 수 있습니다.
        return
    # 정상적인 메시지 처리 로직 (예: consensus_done 업데이트 등)
    logging.info("수신된 메시지가 현재 합의 라운드에 해당합니다. 처리 진행...")

async def simulate_rounds(rounds=3):
    """
    라운드를 지정한 횟수(여기서는 3번)만큼 시뮬레이션합니다.
    각 라운드에서 임의의 메시지를 생성하여 handle_reply()를 호출하고,
    라운드 완료 후 상태 초기화를 통해 다음 라운드를 준비합니다.
    """
    for i in range(rounds):
        logging.info("==== 라운드 %d 시작 (현재 seq=%d) ====", i+1, current_seq)
        
        # 예시: 임의의 메시지를 생성하여 현재 라운드(seq)와 맞는지 점검
        dummy_message = {"seq": current_seq, "data": "dummy reply"}
        handle_reply(dummy_message)
        
        # (실제 시스템에서는 합의 프로세스 동안 여러 메시지를 처리하게 됩니다.)
        # 시뮬레이션을 위해 약간의 지연을 줍니다.
        await asyncio.sleep(1)
        
        logging.info("==== 라운드 %d 완료 (seq=%d) ====", i+1, current_seq)
        # 라운드 완료 후 상태 초기화 및 다음 라운드 준비
        clear_round_state()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(simulate_rounds(3))
