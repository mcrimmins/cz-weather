#!/bin/bash
# cz-weather device diagnostics: run on the CardputerZero (via ssh), output goes to stdout
cd ~/cz-weather || exit 1
echo "== system";   uname -a; head -3 /etc/os-release
echo "== user";     id
echo "== python";   python3 --version
python3 -c 'import PIL; print("Pillow", PIL.__version__)' 2>&1
python3 -c 'import evdev; print("evdev ok")' 2>&1
echo "== fonts";    ls /usr/share/fonts/truetype/dejavu 2>&1 | head -5
echo "== framebuffers"
for f in /sys/class/graphics/fb*; do
  echo "$f size=$(cat $f/virtual_size) bpp=$(cat $f/bits_per_pixel) stride=$(cat $f/stride 2>/dev/null) name=$(cat $f/name 2>/dev/null)"
done
ls -l /dev/fb* 2>&1
echo "== input devices"; grep -E "^N:|^H:" /proc/bus/input/devices 2>&1
ls -l /dev/input/event* 2>&1
echo "== services (launcher?)"
systemctl list-units --type=service --no-pager 2>/dev/null | grep -i -E "launch|app|lvgl|gui|ui\b"
echo "== memory";   free -m
echo "== app file"; ls -l cz_weather.py
echo "== run app for 60 s on /dev/fb0"
timeout 60 python3 cz_weather.py --fb /dev/fb0 2>&1
echo "exit code: $? (124 = stopped by the 60 s timeout, which is normal)"
echo "== errors.log"; tail -n 30 ~/.cache/cz-weather/errors.log 2>&1
