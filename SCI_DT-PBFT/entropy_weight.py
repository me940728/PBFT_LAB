import numpy as np

# 1. 데이터 정규화
def normalize(data):                 # axis=0 열(column, 세로) 기준, axis=1 행(row, 가로) 기준
    normalized_data = (data - data.min(axis=0)) / (data.max(axis=0) - data.min(axis=0))
    return normalized_data

# 2. Pi,j 비율 계산
def calculate_pij(normalized_data):
    pij = normalized_data / normalized_data.sum(axis=0)
    return pij

# 3. 엔트로피 계산
def calculate_entropy(pij, L):
    # 엔트로피 계산, 로그의 0에 대한 처리를 위해 np.where를 사용하여 0인 경우를 제외함
    entropy = - (1 / np.log(L)) * np.sum(np.where(pij != 0, pij * np.log(pij), 0), axis=0)
    return entropy

# 4. 가중치 계산
def calculate_weights(entropy):
    weights = (1 - entropy) / np.sum(1 - entropy)
    return weights

# 평가 샘플과 지표 값 (샘플 3개, 지표 2개)
data = np.array([[10, 15], [20, 25], [30, 35]])

# 1. 정규화된 데이터
normalized_data = normalize(data)

# 2. Pi,j 비율
pij = calculate_pij(normalized_data)

# 3. 엔트로피 계산 (L = 샘플 수)
L = normalized_data.shape[0]
entropy = calculate_entropy(pij, L)

# 4. 지표 가중치 계산
weights = calculate_weights(entropy)

# 결과 출력
print("정규화된 데이터:")
print(normalized_data)
print("\nPi,j 비율:")
print(pij)
print("\n엔트로피 값:")
print(entropy)
print("\n지표 가중치:")
print(weights)
