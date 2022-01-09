#!/usr/bin/env python3

import sys
import struct

nf = int(sys.argv[1])
tag = sys.argv[2]

f = []
for n in range(nf):
	f.append(open(f"{tag}{n}", "rb"))

while True:
	bs = []
	for n in range(nf):
		# print(f"mux: read {tag}{n} ...", file=sys.stderr)
		b = f[n].read(1024)
		# print(f"mux: read {tag}{n}, {len(b)} bytes", file=sys.stderr)
		if not b:
			bs = None
			break
		
		bs.append(b)
	
	dec = [y for x in zip(*bs) for y in x]
	dec = struct.pack(f"{len(dec)}B", *dec)
	sys.stdout.buffer.write(dec)
