# test py
import numpy as np

data = np.array([[10, 20], [20, 30], [30, 40]])

print(data)
print("------")
for i in data:
    for j in data[i] :
        print(data[i][j])
