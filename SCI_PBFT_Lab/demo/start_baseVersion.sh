#/bin/bash

k="4"
node="400"
#r="2"
delay=1000
iter_cnt=1
cluster="4"

for node in {"120","160","200","240","280","300"}
do
    if [ $node = "120" ]
    then
        delay=30
    elif [ $node = "160" ]
    then
        delay=50
    elif [ $node = "200" ]
    then
        delay=70
    elif [ $node = "240" ]
    then
        delay=110
    elif [ $node = "280" ]
    then
        delay=130
    elif [ $node = "300" ]
    then
        delay=150
    fi

    for r in {2,4,6,8,10,12,14,16,18,20}
    do
        for cluster in {"4","5","6"}
        do
            echo $iter_cnt
            for ((j=0;j<=$iter_cnt;j++))
            do
                node_path="./yaml_file/"${k}"/"${node}"/"
                node_file=${node_path}"node_info_"${r}"r_"${node}".yaml"
                cpn_path="./yaml_file/CPN/"
                cpn_file=${cpn_path}${cluster}"_cluster.yaml"
                echo $file

                echo $delay

                pkill -9 python
                rm *.blockchain
                rm *.log
                sleep 2

                int=$((node))
                iter=$(($int))

                # NN - node file
                c_int=$((cluster))
                for ((i=0; i<$iter; i++))
                do
                    python ./node_group.py  -c $node_file -cn $c_int -i $i -lf True &
                done

                sleep 3
                # CPN - node file
                for ((i=0; i<$c_int; i++))
                do
                    python ./cpn.py -c $node_file -cn $c_int -id $i &
                done

                sleep 3
                # PN - cpn file
                for i in {0..0}
                do
                    python ./client_group.py -cn $c_int -c $cpn_file -id $i -nm 5 -nr $((r))&
                done
                sleep $delay
            done
        done
    done
done
