# cz-weather

An Open-Meteo forecast viewer for the M5Stack CardputerZero's 320×170 screen.
It runs on Python 3 with Pillow and needs no API key.

**Screens.** There are 18, in six sections. Press ← → (or `,` `/`) to move through them, or `1`–`6` to jump to a section:

| # | Section | Screens |
|---|---|---|
| 1 | Forecast | **Now** (conditions + 24 h sparkline) · **Temp** 48 h T/Td · **Precip** 48 h chance and amount · **Wind** 48 h · **7-day** |
| 2 | Climate | **Clim T** daily Tmax/Tmin vs 1991–2020 p10–p90 bands · **WY Precip** water year vs climatology · **Anomalies** summary card |
| 3 | Monsoon | **Monsoon**: 72 h dewpoint (54°F line), precipitable water (1 in line), CAPE · **Clim Td** daily mean dewpoint vs climatology |
| 4 | Fire | **Fire Wx**: 72 h RH (15% line), wind/gusts, VPD, with "critical" hours shaded · **Clim RHmin**, **Clim VPD**, **Clim Gust** |
| 5 | Ag | **GDD** since Jan 1 vs climatology (base 50/86°F) · **Clim ET0** · **Clim SWrad** |
| 6 | Sounding | **Sounding**: skew-T-style T/Td profile (°C) with dry adiabats, winds at 850–250 hPa (kt), PW, CAPE, LI, freezing level and 500 hPa height. Press ↑ ↓ (or `;` `.`) to step the valid time ±3 h across 3 days |

All the "Clim" screens use the same layout: the last 45 days plus the next 16, the shaded p10–p90 band, the median
line, and a 30-day anomaly in the legend. Reanalysis days are drawn solid and forecast-model days dashed.

The Fire Wx shading marks hours with RH ≤ 15% *and* sustained wind ≥ 20 mph or gusts ≥ 35 mph. It's an
illustrative threshold, not an official Red Flag criterion. Change `FIRE_RH`, `FIRE_WIND_MPH` and `FIRE_GUST_MPH` in the code to match
local NWS criteria.

### Site screen (where the numbers come from)

The last Forecast screen, **Site**, shows the place itself (coordinates, plus elevation and population when it came from a
name search). Below that is one row per Open-Meteo source: the forecast, the climatology reanalysis and the hourly data
(live or archive). Each row has the grid-cell center the data actually comes from, its distance from your location,
and the elevation Open-Meteo downscales the values to (from a 90 m DEM). Open-Meteo returns no cell size, so the
climatology row shows the reanalysis' nominal resolution. The forecast is a blend of models, so it has no single
resolution to show. Climatologies cached before this screen existed get their grid info with one tiny request.

### Reading values off a plot (inspect)

Press **Enter** on any plot screen to show a gold cursor. `fn`+`Z` / `fn`+`C` (← →) move it one step (one hour or one day);
`fn`+`F` / `fn`+`X` (↑ ↓) jump 6 hours or a week. The readout shows the date or time and every value at the cursor. On the climate
screens that's the observed value with the day's p10/p50/p90 (and the anomaly on the single-variable Clim screens). Press Enter
or Esc to leave inspect mode; the arrows then change screens again. The Now, 7-day, Anomalies and Sounding screens already
show their numbers as text, so they have no cursor.

### Setting the location

Press `l`, then type either a place name (`Flagstaff`, `Silver City NM`) or coordinates (`32.25, -110.95`)
and press Enter. For a name, pick from the matches with the arrow keys (Fn + `;` `.` on the Cardputer) and press Enter again. In the search box, `;` and `.` type as ordinary characters, so coordinates come out right. From the command line, use
`--location "Willcox"` or `--location "31.9,-109.8"`. The location is saved and used the next time the app starts.

### Viewing a past date

The app always starts on today. Press `d` and type a date (`2021-07-23`; Enter alone returns to today), or use
`b` / `n` (or `-` / `=`) to step a week back or forward. Stepping from a Forecast page jumps to Climate so you see the change. Every screen header carries a date tag, so you can always tell what you're
looking at:

| Screens | What the date does | Tag |
|---|---|---|
| Forecast (Now, Temp, Precip, Wind, 7-day) | always today | gray "today 24 Nov"; the Now screen shows the clock, plus "viewing …" when a past date is set |
| Climate, Clim *, GDD | 45 days before and 16 after the date; that water or calendar year; anomalies for that date | gold "20 Jul 2023" for a past date, gray "today …" otherwise |
| Monsoon, Fire Wx, Sounding | 3 days from 00:00 on that date, from Open-Meteo's historical-forecast archive (2022 onward) | gold date; before 2022 the screen says the archive isn't available |

"Today" is the date at the selected location, from the forecast's timezone, not the device clock, which may be on UTC.

What a new date costs:

- **1991–2020:** nothing. The raw 30-year baseline is kept on the device (`base2_*.json`, about 1 MB).
- **Other years (1940 onward):** one archive request per calendar year, about 25 API calls, cached
  permanently (`year2_*.json`). The current year refreshes every 3 hours.
- **Monsoon, Fire Wx and Sounding for a past date:** one small archive request (2–3 calls), cached permanently.
- **Changing location:** the climatology downloads once for the new location (about 700 calls), and then the same rules apply.

From the command line, `python cz_weather.py --png out --date 2021-07-23` renders all screens for that date.

### How the climatology works

Everything comes from Open-Meteo, so it works for any point:

- **Model:** the app uses ERA5-Seamless (ERA5-Land temperature and humidity plus ERA5 radiation) if the archive
  API accepts it, and falls back to ERA5-Land otherwise.
- **Which variables get a climatology:** Tmax, Tmin and precipitation always do. The app checks each optional daily
  variable (mean dewpoint, minimum RH, maximum VPD, maximum gust, ET0, shortwave radiation) with a one-week
  test request and keeps only those the archive actually returns at that point. A missing variable shows
  "not available" on its screen and nothing else breaks.
- **Baseline:** one archive request covers Oct 1990–Dec 2020. The device computes day-of-year p10/p50/p90 and the
  mean, pooled over a ±7-day window, along with records, water-year precipitation curves and Jan 1 GDD curves for
  1991–2020. This runs once per location and units, and the result (~250 KB) is cached in
  `~/.cache/cz-weather/clim2_*.json`. On the CM0, expect the first build to take somewhere between several seconds and a minute.
- **Recent:** the same reanalysis from the archive API, which runs about 5 days behind, starting Jan 1 so GDD
  works. The gap up to today is filled from the forecast API's `past_days`, followed by the 16-day forecast. This
  refreshes every 3 hours, or when you press `r`.
- Anomalies are measured against the smoothed daily climatological mean. Values describe a ~9–25 km grid cell,
  not a station.

### Hourly profiles (Monsoon, Fire, Sounding)

These three screens use a separate 3-day hourly request with 14 pressure levels from 1000 to 200 hPa plus surface fields.
It's always fetched in metric and converted for display, and it refreshes hourly. Pressure-level hourly data has no
archive equivalent, so these screens have no climatology. Their daily counterparts (Clim Td, Clim RHmin and so on)
cover that. Precipitable water, lifted index and CIN are optional: if the API rejects one of those names, the
app drops it and still shows the rest.

**Keys:** ← → (or `,` `/`) change screen · `1`–`6` jump to a section · ↑ ↓ (or `;` `.`) step time on the Sounding screen ·
`h` key list · `Enter` inspect · `d` view a past date · `b` `n` (or `-` `=`) step the date a week · `r` refresh everything · `l` set the location · `u` switch imperial/metric · `q` or hold `Esc` 2 s quit

The forecast refreshes every 15 minutes. The last good forecast for each location is saved in
`~/.cache/cz-weather/`, so the app still shows data when you're offline, with a red "offline" note.
Your chosen location and units are stored in `~/.config/cz-weather.json`. Tucson is the default.

## Try it on your VM first

```bash
pip install pillow            # Tk usually comes with Python; otherwise: sudo apt install python3-tk
python3 cz_weather.py --window                # interactive preview at 3x scale
python3 cz_weather.py --png out/              # write all 18 screens as PNGs
python3 cz_weather.py --location "Flagstaff" --window
```

## On the CardputerZero

```bash
sudo apt install python3-pil python3-evdev fonts-dejavu-core
sudo usermod -aG video,input $USER     # log out and back in afterwards

# find the framebuffer that is 320x170 (fb0 may be HDMI)
for f in /sys/class/graphics/fb*; do echo "$f $(cat $f/virtual_size) $(cat $f/bits_per_pixel)bpp"; done

python3 cz_weather.py --fb /dev/fb0                 # keys come from the built-in keyboard
python3 cz_weather.py --fb /dev/fb0 --keys stdin    # over SSH: type keys in your terminal
```

**Caveats.** I haven't tested this on the hardware. The parts most likely to need adjusting:

- **Framebuffer format.** The code handles 16-bit (RGB565) and 32-bit framebuffers and reads the size and stride
  from sysfs. If the colors look swapped (red and blue reversed), change `"BGR;16"` to `"RGB;16"` in
  `FramebufferDisplay.show`.
- **Sharing the screen with the launcher.** The APPLaunch home screen may redraw over the app. Either start the app from the
  launcher (see below) or stop the launcher while you test.
- **Arrow keys.** The code accepts real arrow keycodes as well as the Cardputer-style `,` `/` `;` `.`, so it should work
  whether or not Fn is held.
- **Unconfirmed API names.** I took the variable names from Open-Meteo's docs, but a few are listed there only by display
  name: `total_column_integrated_water_vapour`, `lifted_index`, `convective_inhibition`, and daily
  `dew_point_2m_mean`, `relative_humidity_2m_min` and `vapour_pressure_deficit_max`. The probing and fallbacks
  handle a wrong name, but if a screen says "not available", that's the first thing to check.

## Adding it to the launcher

The `launcher/` folder holds a wrapper (`run-cz-weather`), a desktop entry, an icon and an install script.
They follow the same layout as zeroSDR, under `/usr/share/APPLaunch/`.

From the laptop (PowerShell, with `$cz = "user@192.168.0.208"`):

```powershell
scp -r cz_weather.py launcher "${cz}:~/cz-weather/"
ssh -t $cz "bash ~/cz-weather/launcher/install_launcher.sh"
```

The script asks for your sudo password, installs the app, sizes the icon to match the other launcher icons,
and restarts the launcher. **CZ Weather** then appears on the home screen.

- **Exit:** `q`, or hold `Esc` for 2 seconds (the launcher convention). A short `Esc` press shows that hint.
- **Logs:** `~/.cache/cz-weather/app.log` (Python output) and `errors.log` (API errors).
- **Update:** copy the new `cz_weather.py` over and rerun the install script.
- **Remove:** `bash ~/cz-weather/launcher/install_launcher.sh --remove`.

## Ideas for v2

- Optional ACIS mode for station sites (actual observations, NCEI normals, period-of-record extremes)
- Chill hours (Nov–Feb) against climatology
- A model-comparison screen showing GFS, ECMWF and HRRR temperatures on one plot
- A saved-locations list (home, a camp spot, a field site) to cycle through with a key
- A screen for your local rtl_433 sensor alongside the forecast
