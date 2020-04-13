kxt encodes/transfers files as keyboard inputs, intended for filetransfer into a vm or vnc/rdp

* crossplatform: windows/linux/osx host, virtually any guest
* stops transfer if you switch to another window (on windows/linux)
* if target is linux/osx: automatic md5-check and unpacking
* option 1) type out the file contents as plaintext (works anywhere)
* option 2) compress the file and send as base64 (most efficient)
* option 3) compress multiple files in a .tar.gz (most convenient)

# examples

send two files and a folder into a linux or osx vm:

    ./kxt.py file1 file2 folder1

send a file into a windows vm:

    ./kxt.py -w file1

type a plaintext document into any vm or remote-desktop window:

    ./kxt.py -p file1

# speed estimates

sorted best to worst, first speed is plaintext, second speed is compressed ascii

* from linux host into qemu/libvirt (-t2) = 0.57 kB/s, 1.31 kB/s
* from windows host into virtualbox (-t1) = 0.23 kB/s, 0.89 kB/s
* from linux host into virtualbox (-t7) = 0.20 kB/s, 0.52 kB/s
* from osx host into virtualbox (-t0) = 0.12 kB/s, 0.36 kB/s

# platform-specific notes

* osx: your keyboard and mouse is effectively disabled until the transfer finishes
* linux: please use xdotool (pynput breaks when target is virtualbox/qemu/libvirt)

# is this overengineered?

absolutely! here is a replacement bash onliner, assuming linux host and guest:

    tx() { sleep 1; (echo 't=$(mktemp);awk "/^$/{exit}1"|base64 -d>$t'; tar -cz "$@" | tee >(md5sum | cut -c-32 >/dev/shm/kxt) | base64 -w72; echo; echo 'echo "'$(cat /dev/shm/kxt)' *$t"|md5sum -c&&tar -zxvf $t&&rm -f $t') | tr '\n' '\r' | xdotool type --file - --delay 8; }

usage: `tx file1 file2 ...`, starts after 1sec, does md5-check but keeps going even if you switch focus

# todo

* add zip support (removes dependency on 7zip in windows guests)
* add network transport (with keyboard sim just for setup)
* support single-file input from stdin
