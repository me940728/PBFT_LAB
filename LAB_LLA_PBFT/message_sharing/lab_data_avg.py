import os
import yaml # type: ignore
import json

# 전역 설정 변수
PROTOCOLS = ["pbft", "lla_pbft", "random_pbft"]
INPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lab_log/anal")
# OUTPUT_FORMAT 값을 "yaml" 또는 "json"으로 설정하면 해당 포맷으로 결과 파일이 생성
OUTPUT_FORMAT = "json"  # 또는 "yaml"
#OUTPUT_FORMAT = "yaml"  # 또는 "json"
# 출력 파일 이름(확장자는 포맷에 따라 자동 적용)
OUTPUT_FILE_NAME = "consensus_avg." + ("json" if OUTPUT_FORMAT == "json" else "yaml")

output_file = os.path.join(INPUT_DIR, OUTPUT_FILE_NAME)

result = {}

# 각 프로토콜 파일을 순회하며 평균 계산
for protocol in PROTOCOLS:
    file_path = os.path.join(INPUT_DIR, f"{protocol}_consensustime.yaml")
    if not os.path.exists(file_path):
        print(f"파일이 존재하지 않습니다: {file_path}")
        continue

    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # 결과 딕셔너리 초기화
    result[protocol] = {}
    # 예: data 구조: { "50m": { "20d": [값, ...], "40d": [값, ...], ... }, "100m": { ... }, ... }
    for coverage_key, drone_dict in data.items():
        result[protocol][coverage_key] = {}
        for drone_key, values in drone_dict.items():
            if values:
                avg = sum(values) / len(values)
            else:
                avg = None
            result[protocol][coverage_key][drone_key] = avg

# 결과 저장: 전역 변수 OUTPUT_FORMAT에 따라 YAML 또는 JSON 형식으로 저장
if OUTPUT_FORMAT.lower() == "json":
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4, ensure_ascii=False)
elif OUTPUT_FORMAT.lower() == "yaml":
    with open(output_file, "w", encoding="utf-8") as f:
        yaml.dump(result, f, allow_unicode=True)
else:
    raise ValueError("OUTPUT_FORMAT은 'json' 또는 'yaml'이어야 합니다.")

print("평균 계산 결과가 저장되었습니다:", output_file)
