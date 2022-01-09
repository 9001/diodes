#!/usr/bin/env python3

import sys

nf = int(sys.argv[1])
tag = sys.argv[2]

f = []
for n in range(nf):
	f.append(open(f"{tag}{n}", "wb"))

rem = b""
while True:
	buf = sys.stdin.buffer.read(4096)
	# print(f"demux: read {len(buf)} bytes", file=sys.stderr)
	if buf:
		buf = rem + buf
		rem = b""
		skip = len(buf) % nf
		if skip:
			rem = buf[-skip:]
			buf = buf[:-skip]
	elif not rem:
		# print(f"demux: no buf, no rem, eof", file=sys.stderr)
		break
	else:
		buf = rem
		rem = b""
	
	for n in range(nf):
		b = buf[n::nf]
		# print(f"demux: writing {len(b)} bytes to {n}", file=sys.stderr)
		f[n].write(b)
