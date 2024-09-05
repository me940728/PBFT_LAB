for i in {0..1}
do
   python ./client.py -id $i -nm 5 & # 백그라운드에서 파이썬 파일은 2번 동작 시킴
done
