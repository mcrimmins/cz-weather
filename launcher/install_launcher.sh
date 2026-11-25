#!/bin/bash
# Install or update cz-weather in the CardputerZero launcher (APPLaunch).
#   bash ~/cz-weather/launcher/install_launcher.sh            install / update
#   bash ~/cz-weather/launcher/install_launcher.sh --remove   uninstall
# Run it again after copying a new cz_weather.py to ~/cz-weather/.
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/usr/share/APPLaunch

restart_launcher() {
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
    if systemctl --user restart APPLaunch.service 2>/dev/null; then
        echo "launcher restarted"
    else
        echo "could not restart APPLaunch.service; reboot the Cardputer to see the change"
    fi
}

if [ "$1" = "--remove" ]; then
    sudo rm -f "$APP/bin/cz_weather.py" "$APP/bin/run-cz-weather" \
               "$APP/applications/cz-weather.desktop" "$APP/share/images/cz-weather.png"
    echo "cz-weather removed (your caches in ~/.cache/cz-weather are kept)"
    restart_launcher
    exit 0
fi

[ -d "$APP" ] || { echo "no $APP: is this the CardputerZero?"; exit 1; }
for f in cz_weather.py launcher/run-cz-weather launcher/cz-weather.desktop launcher/cz-weather.png; do
    [ -f "$SRC/$f" ] || { echo "missing $SRC/$f"; exit 1; }
done

echo "installing from $SRC"
sudo install -d "$APP/bin" "$APP/applications" "$APP/share/images"
sudo install -m 644 "$SRC/cz_weather.py"               "$APP/bin/cz_weather.py"
sudo install -m 755 "$SRC/launcher/run-cz-weather"     "$APP/bin/run-cz-weather"
sudo install -m 644 "$SRC/launcher/cz-weather.desktop" "$APP/applications/cz-weather.desktop"

# size the icon like the launcher's other app icons (most common size; 64 px if none found)
SIZE=$(python3 - "$APP/share/images" <<'PY'
import collections, glob, os, sys
from PIL import Image
sizes = collections.Counter()
for p in glob.glob(os.path.join(sys.argv[1], "*.png")):
    name = os.path.basename(p)
    if name.startswith(("lockscreen", "cz-weather")):
        continue
    try:
        w, h = Image.open(p).size
    except Exception:
        continue
    if w == h and w <= 256:
        sizes[w] += 1
print(sizes.most_common(1)[0][0] if sizes else 64)
PY
)
python3 -c "from PIL import Image; Image.open('$SRC/launcher/cz-weather.png').resize(($SIZE, $SIZE), Image.LANCZOS).save('/tmp/cz-weather-icon.png')"
sudo install -m 644 /tmp/cz-weather-icon.png "$APP/share/images/cz-weather.png"
echo "icon installed at ${SIZE}x${SIZE}"

echo "installed:"
ls -l "$APP/bin/cz_weather.py" "$APP/bin/run-cz-weather" "$APP/applications/cz-weather.desktop" \
      "$APP/share/images/cz-weather.png"
restart_launcher
echo "look for 'CZ Weather' on the home screen. Logs: ~/.cache/cz-weather/app.log"
