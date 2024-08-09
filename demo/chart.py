
import matplotlib.pyplot as plt

# 15개 항목 데이터와 항목 이름 정의
data = [1154, 433, 248, 241, 230, 109, 104, 43, 23, 59]
labels = [
    '지체', '청각', '시각', '뇌병변', '지적',
    '신장', '정신', '자폐성', '언어', '장루 등 6개'
]

# 원형 그래프 생성
plt.figure(figsize=(10, 7))  # 그림의 크기 설정
plt.pie(data, labels=labels, autopct='%1.1f%%', startangle=140)
plt.title('2023년 등록 장애인수')
plt.axis('equal')  # 원형 그래프를 원으로 표시

# 그래프 출력
plt.show()
