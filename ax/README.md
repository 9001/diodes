~52 KiB/s over stereo 96khz
* that's kibibyte, so 8 times faster than 56k modems
* needs `quiet-encode` and `quiet-decode` (the 44k1 edition) from https://github.com/9001/lxc/tree/hovudstraum/static-quiet


## usage

before you start, you may want to
```
sudo tee -a /etc/pulse/daemon.conf <<'EOF'
default-sample-format = float32le
default-sample-rate = 96000
EOF
pulseaudio -k
systemctl --user restart pulseaudio.service
pulseaudio --restart
```

regardless, check your soundcard samplerate and adjust the below `r` variables to min(sender,receiver):
```
pactl list short sinks
```

* send to soundcard (transmitter):
  ```
  r=96000; cat some.tgz | ./axt.py | pacat -p --raw --format=float32le --rate=$r --channels=2 --no-remix
  ```
  
  but if you have `ffmpeg` you should do this instead:
  ```
  r=96000; cat some.tgz | ./axt.py | ffmpeg -v warning -ar $r -ac 2 -f f32le -i - -f f32le - -filter_complex "[a:0]showspectrum=s=1024x576:fps=30:legend=1:slide=scroll:color=intensity:fscale=lin:orientation=horizontal,crop=1280:640,format=yuv420p[vo]" -map "[vo]" -f sdl 'encoder output' | pacat -p --raw --format=float32le --rate=$r --channels=2 --no-remix
  ```

* decode from soundcard (receiver):
  ```
  r=96000; pacat -r --raw --format=float32le --rate=$r --channels=2 --no-remix | ffmpeg -v warning -ar $r -ac 2 -f f32le -i - -f f32le - -filter_complex "[a:0]showspectrum=s=1024x576:fps=30:legend=1:slide=scroll:color=intensity:fscale=lin:orientation=horizontal,crop=1280:640,format=yuv420p[vo]" -map "[vo]" -f sdl 'decoder input' | ./axr.py | pv -Wapterbi 0.5 > dec.bin
  ```

  and the recommended ffmpeg alternative:
  ```
  r=96000; pacat -r --raw --format=float32le --rate=$r --channels=2 --no-remix | ./axr.py | pv -Wapterbi 0.5 > dec.bin
  ```

the `pv` part (shows transfer speed) is optional and can be replaced with `cat`


## notes

* the volume readout in the receiver should never exceed 80%
* audio must be headerless f32le
* depending on soundcard, you may need:
  * `num_subcarriers` = `256`
  * `mod_scheme` = `arb256opt` or `qam256`
  * `inner_fec_scheme` = `v29p23` or `v27p23` (instead of `none`)
* `ps aux | awk '/pacat[ ]|python3..ax[tr].py|quiet-[de][en]code[ ]/{print$2}' | xargs kill`

## known issues

* crashes if input is shorter in bytes than the number of channels
  * wontfix because nobody would ever transfer one single byte
