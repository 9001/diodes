#!/bin/bash
set -e

# ./test.sh < ~/Videos/dashcam.webm | tee dec | pv -apterb | cmp ~/Videos/dashcam.webm 
# 54 KiB/s 2ch 96khz

nch=2
p=w
re=-re
#re=
exec 123>&0
exec 0>/dev/null

end() {
	ps aux | awk '/bash[ ]..tx.sh$|cat[ ]header|tee[ ]ch|ffmpeg[ ]-v warning|sed[ ].*e[123]: |tail[ ]-Fn100 e|ffmpeg[ ]-re -v fatal/{print$2}' | xargs kill 
}
trap end EXIT

fia=()
vfa=
foc=()

for ((a=0;a<$nch;a++)); do
	rm -f f$a g$a h$a i$a e1 e2 e3
	mkfifo f$a g$a i$a
	fia+=(-f f32le -i g$a)
	vfa="$vfa[$a:a]"
	foc+=(-map_channel 0.0.$a -f f32le tcp://127.0.0.1:543$a/)
	
	(cat f$a | tee src$a | ./quiet-encode $p | tee enc$a >g$a; echo ENCODE ENDED >&2) &
	( (cat header-44khz.wav; ncat -l -p 543$a) | tee rch$a | ./quiet-decode $p /dev/stdin | tee dec$a >i$a; echo DECODE ENDED >&2) &
	# strace -Tttyyvfs 4096 -o quiet$a
	# ( (cat header-44khz.wav; ncat -l -p 543$a) | tee rch$a | pv >h$a) &
	# (./quiet-decode $p h$a | tee dec$a >i$a; echo DECODE ENDED >&2) &
done </dev/null

# (strace -Tttyyvfxs 4096 -o demux ./demux.py $nch f <&123; echo DEMUX ENDED >&2) &
(./demux.py $nch f <&123; echo DEMUX ENDED >&2) &

ffmpeg -v warning "${fia[@]}" -filter_complex "$vfa amerge=inputs=$nch [a]" -map '[a]' -f f32le - 2> >(sed 's/^/e1: /' >&2 </dev/null) |
ffmpeg -v warning $re -ar 96000 -ac $nch -f f32le -i - -f f32le - -filter_complex "[a:0]showspectrum=s=1024x576:fps=30:legend=1:slide=scroll:color=intensity:fscale=lin:orientation=horizontal,crop=1280:640,format=yuv420p[vo]" -map "[vo]" -f sdl - 2> >(sed 's/^/e2: /' >&2) |
ffmpeg -v warning -y -f f32le -ac $nch -i - "${foc[@]}" 2> >(sed 's/^/e3: /' >&2) &
# strace -Tttyyvfs 4096 -o ff

ps aux | grep ffmpeg >&2

./mux.py $nch i </dev/null



exit 0



cat >/dev/null <<'EOF'
# 3ch
/home/ed/Videos/dashcam.webm - differ: byte 12289, line 51

cat ~/Videos/dashcam.webm | dd bs=1 skip=12273 count=64 2>/dev/null | hexdump -C
00000000  3c 10 8f b4 3f 7d 0a c2  98 be 2f 0a 55 b1 f2 d0  |<...?}..../.U...|
00000010  a0 63 3a d8 d9 95 95 fa  04 56 92 05 5c f3 a3 71  |.c:......V..\..q|
00000020  16 cd f9 f9 85 7c 5f a6  aa d2 4c 01 81 5d 5b 4a  |.....|_...L..][J|

cat dec | dd bs=1 skip=12273 count=64 2>/dev/null | hexdump -C
00000000  3c 10 8f b4 3f 7d 0a c2  98 be 2f 0a 55 b1 f2 e5  |<...?}..../.U...|
00000010  ff d0 a0 63 3a d8 d9 95  95 fa 04 56 92 05 5c f3  |...c:......V..\.|
00000020  a3 71 16 cd f9 f9 85 7c  5f a6 aa d2 4c 01 81 5d  |.q.....|_...L..]|

vim demux
/ be..2f..0a..55..b1..f2
# 208699 05:45:52.752205 read(0</home/ed/Videos/dashcam.webm>, [...] \x23\xe9\xbc\xb2 [\xe5\xff] ", 4096) = 4096 <0.000008>
# 208699 05:45:52.752253 read(0</home/ed/Videos/dashcam.webm>, [...] \x7d\x0a\xc2\x98\xbe\x2f\x0a\x55\xb1\xf2", 4096) = 4096 <0.000008>
# 208699 05:45:52.752308 read(0</home/ed/Videos/dashcam.webm>, "\xd0\xa0\x63\x3a\xd8\xd9\x95
EOF



cat >/dev/null <<'EOF'
# 2ch
./test.sh < ~/Videos/dashcam.webm | tee dec | pv | cmp ~/Videos/dashcam.webm 
# /home/ed/Videos/dashcam.webm - differ: byte 552961, line 2163

cat ~/Videos/dashcam.webm | dd bs=1 skip=552945 count=64 2>/dev/null | hexdump -C 
00000000  67 9c 3c 45 29 e3 3c 13  70 75 e2 21 61 af b0 be  |g.<E).<.pu.!a...|
00000010  f1 17 74 96 12 8f 2e e4  40 5e 9f f1 a3 1d 7a e1  |..t.....@^....z.|
00000020  ad 59 0c d8 4e 0f 57 95  f8 b7 cb c8 95 c8 63 a1  |.Y..N.W.......c.|

cat dec | dd bs=1 skip=552945 count=64 2>/dev/null | hexdump -C 
00000000  67 9c 3c 45 29 e3 3c 13  70 75 e2 21 61 af b0 96  |g.<E).<.pu.!a...|
00000010  12 8f 2e e4 40 5e 9f f1  a3 1d 7a e1 ad 59 0c d8  |....@^....z..Y..|
00000020  4e 0f 57 95 f8 b7 cb c8  95 c8 63 a1 d1 c8 bd b0  |N.W.......c.....|

hexdump -C ~/Videos/dashcam.webm | grep 'f1 17 74 96' -C1
00086ff0  3e 67 9c 3c 45 29 e3 3c  13 70 75 e2 21 61 af b0  |>g.<E).<.pu.!a..|
00087000  be f1 17 74 96 12 8f 2e  e4 40 5e 9f f1 a3 1d 7a  |...t.....@^....z|
00087010  e1 ad 59 0c d8 4e 0f 57  95 f8 b7 cb c8 95 c8 63  |..Y..N.W.......c|

pv ~/Videos/dashcam.webm | od -w2 -vtx1 | awk '{printf "\\x%s", $2} NR%64==0{print""}' | while IFS= read -r x; do printf "$x"; done > src0r
pv ~/Videos/dashcam.webm | od -w2 -vtx1 | awk '{printf "\\x%s", $3} NR%64==0{print""}' | while IFS= read -r x; do printf "$x"; done > src1r

cmp src0{,r}; cmp src1{,r}; 
src0 src0r differ: byte 276481, line 1102
src1 src1r differ: byte 276482, line 1062

vim demux
/ .x12.x8f.x2e.xe4.x40.x5e.x9f.xf1
# 205499 05:05:20.249914 read(0</home/ed/Videos/dashcam.webm>, [...] \x13\x70\x75\xe2\x21\x61\xaf\xb0", 4096) = 4096 <0.000007>
# 205499 05:05:20.764098 read(0</home/ed/Videos/dashcam.webm>, "\x96\x12\x8f\x2e\xe4\x40\x5e
/ .x8f.xe4.x5e.xf1                                                               __  __  __  __
# 205499 05:05:20.764874 write(3</home/ed/dev/diodes/ax/f0>, [...] \x21\xaf\x96\x8f\xe4\x5e\xf1\x1d\xe1\x59\xd8\x0f
/ .x12.x2e.x40.x9f                                                               __  __  __  __
# 205499 05:05:20.764961 write(4</home/ed/dev/diodes/ax/f1>, [...] \xe2\x61\xb0\x12\x2e\x40\x9f\xa3\x7a\xad\x0c\x4e  

f=~/Videos/dashcam.webm; rm f0 f1; mkfifo f0 f1; cmp < <(od -w2 -vtx1 $f | awk '{printf "\\x%s", $2} NR%64==0{print""}' | while IFS= read -r x; do printf "$x"; done) f0 & cmp < <(od -w2 -vtx1 $f | awk '{printf "\\x%s", $3} NR%64==0{print""}' | while IFS= read -r x; do printf "$x"; done) f1 & pv $f | ./demux.py 2 f
34.6MiB 0:00:16 [2.09MiB/s] [==============================================================================================================================================>] 100%            
cmp: EOF on - after byte 18117568, in line 71155

EOF
