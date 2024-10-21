import openpyxl

# 엑셀 파일 경로 설정 (파일 경로에 맞게 수정 필요)
file_path = '/Users/admin/Downloads/ll/envs/pbft/source/KCI_LAB/origin_data/YELLOWDUST-1_1.xlsx'

# 전역 변수 정의
data_json = []
gloss_id_list = []
start_list = []

# 엑셀 파일 열기
workbook = openpyxl.load_workbook(file_path)
sheet = workbook.active

# A~AZ까지의 알파벳 생성
columns = [chr(i) for i in range(ord('A'), ord('Z') + 1)]
columns += [f'A{chr(i)}' for i in range(ord('A'), ord('Z') + 1)]

# Korean sentence 임시 저장 변수
kor_sentence = None

# 엑셀 순회
for row in range(1, sheet.max_row + 1):
    row_data = [sheet[f'{col}{row}'].value for col in columns]
    row_data = [val for val in row_data if val is not None]

    if not row_data:
        continue

    first_cell = row_data[0]

    if first_cell == 'Korean sentence : ':
        # 이전에 저장된 데이터로 JSON 생성
        if kor_sentence is not None:
            # start_list와 gloss_id_list 정렬
            sorted_pairs = sorted(zip(start_list, gloss_id_list))
            sorted_start, sorted_gloss_id = zip(*sorted_pairs) if sorted_pairs else ([], [])
            # ksl 값 생성
            ksl_value = ' '.join(sorted_gloss_id)
            # JSON 데이터 생성
            data_json.append({
                'kor': kor_sentence,
                'ksl': ksl_value,
                'time': list(sorted_start)
            })
        # 새 Korean sentence 저장
        kor_sentence = row_data[1] if len(row_data) > 1 else None
        # start_list 초기화
        start_list = []

    elif first_cell == 'gloss_id : ':
        # gloss_id 값 추가
        for value in row_data[1:]:
            if value not in [None, '']:
                gloss_id_list.append(value)

    elif first_cell == 'start(s) : ':
        # start 값 추가
        for value in row_data[1:]:
            if value not in [None, '']:
                start_list.append(value)

# 마지막 Korean sentence에 대한 JSON 생성
if kor_sentence is not None:
    # start_list와 gloss_id_list 정렬
    sorted_pairs = sorted(zip(start_list, gloss_id_list))
    sorted_start, sorted_gloss_id = zip(*sorted_pairs) if sorted_pairs else ([], [])
    # ksl 값 생성
    ksl_value = ' '.join(sorted_gloss_id)
    # JSON 데이터 생성
    data_json.append({
        'kor': kor_sentence,
        'ksl': ksl_value,
        'time': list(sorted_start)
    })

# 결과 출력
for data in data_json:
    print(data)
    
print(start_list)