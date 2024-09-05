# test code python
import json
import hashlib

# [1-1] test
hash_object = hashlib.md5(json.dumps("message", sort_keys=True).encode())
key = ("1", hash_object.digest()) # => View객체에서 뷰 넘버를 가져와서 해시 키를 생성
print(key)