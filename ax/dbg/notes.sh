#!/bin/bash
exit 1

# desync ch0 magic b4c3 sz 58854 hex e5e6
# \xb4\xc3\xe5\xe6\x67\x25\xb7\x7d\x65\x20\xae\xa9\x62\x6a\x9d\xbe\x75\x5f\xcc\xd3\x54\x18\x1e\x0f\x3b\xa3\x76\xed\x78\x19\x76\x07\xbf\xed\x3c\xce\xb7\x19\x01\xc7\x48\xea\xd6\x89\xfb\xca\x79\x58\x86\xc4\x28\x04\xec\xdb\x32\x8e\xa1\x63\x18\xce\x97\xe1\x0a\x7a

cat header-44khz.wav 0.rp | ./quiet-decode w /dev/stdin > r
cmp 0.rd < <(tail -c+66 r)
# cmp: EOF on 0.rd after byte 36799, in line 169

cmp 0.rd 0.td
# 0.rd 0.td differ: byte 7936, line 39

cat dbg/header-44khz.wav 0.tp | ./quiet-decode w /dev/stdin > t
cmp 0.td < <(tail -c+66 t)
# cmp: EOF on - after byte 147391, in line 619

cmp 0.rp 0.tp
0.rp 0.tp differ: byte 126977, line 450
# /4 = sample 31744 (verified in audacity)

for a in 0 1; do ffmpeg -y -f f32le -ac 2 -i pcm -map_channel 0.0.$a -f f32le $a.ff; cat dbg/header-44khz.wav $a.ff | ./quiet-decode w /dev/stdin > $a.ffd; done
cmp 0.tp 0.ff
# 0.tp 0.ff differ: byte 126977, line 450
# lost the next 31744 samples = 126976 bytes = 0x1f000 = 124 KiB
