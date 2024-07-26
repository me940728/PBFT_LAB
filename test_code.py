# test code python
# 3461500, 1600000 18
import json
import hashlib

# [1-1] test
hash_object = hashlib.md5(json.dumps("message", sort_keys=True).encode())
key = ("1", hash_object.digest()) # => View객체에서 뷰 넘버를 가져와서 해시 키를 생성
print(key)

print(2 % 3)

status_by_slot = {}
status_by_slot = status_by_slot[0].commit_certificate
print(status_by_slot)