# cz-weather — project notes for Claude Code

## What this is
A single-file Python app (`cz_weather.py`) that shows Open-Meteo forecasts, a 1991–2020 reanalysis
climatology, and hourly monsoon/fire/sounding profiles on the M5Stack CardputerZero's 320×170 LCD.
The CardputerZero is a Raspberry Pi CM0 handheld (quad Cortex-A53, **512 MB RAM**) running Linux.
Owner: Mike, a climatologist (University of Arizona Cooperative Extension). He works in R day to day
and is newer to Python, so explain Python-specific fixes briefly.

## Environments
- **Development:** Windows laptop, VS Code or Positron, Python 3.12/3.13 from python.org (with Tk).
  Use `--window` (Tk preview at 3×) or `--png DIR`. Fonts fall back to Segoe UI/Arial on Windows.
- **Device:** CardputerZero, reached over SSH from the laptop. Run with `--fb /dev/fbN`
  (`--keys stdin` over SSH). Framebuffer and evdev code paths are Linux-only and imported lazily.

## Git workflow (command line only, by Mike's choice)
Repo on GitHub, branch `main`. Mike commits and pushes from the VS Code terminal, not the Source Control panel.
Give him the commands rather than using the GUI:
```
pytest                                   # offline suite must pass first
git status                               # review what changed
git add -A
git commit -m "short summary of the change"
git push
```
Before installing on the device, bump `APP_VERSION` in cz_weather.py. Then update the Cardputer:
`ssh -t $cz "cd ~/cz-weather && git pull && bash launcher/install_launcher.sh"`.
Line endings: `.gitattributes` forces LF (the device runs Linux). Don't remove it.

## Commands
```
pip install -r requirements.txt
pytest                      # offline suite (fake Open-Meteo in tests/fake_api.py) — must stay green
pytest --live -s            # hits the real API; prints which variables/models are accepted; writes out_live/*.png
python cz_weather.py --window
python cz_weather.py --png out
```
After changing drawing code, render PNGs and look at them. Layout bugs (overlapping text, clipped
labels) don't show up as test failures.

## Design constraints
- Only dependency is Pillow (plus python-evdev on the device). Don't add numpy, pandas, matplotlib
  or requests: the CM0 has 512 MB, and imports are slow there.
- Everything draws into a 320×170 PIL image. Fonts are DejaVu (Linux) at 10/12/14/20/44 px.
- Network calls go through `http_json(url)`; the tests monkeypatch it. Keep it that way.
- All network work runs in background threads; `App.draw()` must never block on the network.
- Caches live in `~/.cache/cz-weather/` (forecast, `clim2_*` climatology, `recent_*`), config in
  `~/.config/cz-weather.json`. Bump `CLIM_VERSION` if the climatology format changes.

## Data flow
- **Main forecast:** forecast API, current + hourly + 16 daily days, user units, 15-min refresh.
- **Climatology (`get_climatology`):** `probe_archive` picks a model (`era5_seamless`, falling back to
  `era5_land`) and keeps the `EXTRA_DAILY` variables that return data. One request covers Oct 1990–Dec 2020,
  then on-device day-of-year percentiles (±7-day pooling), water-year curves and Jan-1 GDD curves.
- **Recent (`get_recent`):** archive from Jan 1 (or earlier for the water year), plus forecast
  `past_days=10` and 16 forecast days. Rows are tagged `src: obs|fc`; plots draw `fc` dashed.
- **Upper (`get_upper`):** 3-day hourly surface and pressure-level fields, always metric, converted
  at draw time. Names in `UPPER_OPTIONAL` are dropped one at a time if the API returns 400.

## API rate limits (learned from the first live run)
Open-Meteo's free tier is about 600 calls/min, 5,000/hour and 10,000/day, and a big request counts as many
calls. One 30-year, 9-variable daily request is roughly 700 calls, and it triggered `HTTP 429`. So
`http_json` waits 61 s and retries on 429 (up to 3 times), the climatology downloads in three
decade chunks, and the live tests cache in `.cache_live/`. Don't add anything that re-downloads the
baseline casually. Settled so far: the archive accepts `models=era5_seamless`.

## View date
`App.view_date` (None = today, never persisted). `get_window(loc, metric, extras, model, center)` builds
the daily rows around it from three sources, in this order: the raw baseline (`base2_*.json`, for 1990-10 to 2020)
with no download; yearly archive caches (`year2_*.json`, permanent for complete years); and the forecast API
only for days the reanalysis hasn't reached yet. Screens read `rec["today"]` as the center date, and
`rec["view_past"]` drives the gold header tag (`view_tag`).

## Date and label conventions (keep consistent)
- Every screen header has a date tag (`header(..., tag=)`). Gray "today D Mon" means today's or live data;
  gold "D Mon YYYY" means a past view date. Forecast pages always use `today_tag(data)`. Climate pages use
  `view_tag(rec)`, hourly pages use `upper_tag(upper, data)`, and placeholders get the tag from `render(view_date=)`.
- "Today" is location-local: `App.local_today()` reads the forecast's current time. Pass it to
  `get_window(..., today=)` and `get_upper(..., today=)`; don't use `date.today()` for app logic.
- Past hourly data: `get_upper(loc, view, today)` uses historical-forecast-api (2022 onward, `HIST_HOURLY_START`),
  starts at 00:00, has no "now" marker (`upper_window` returns `marker=None`), and Sounding steps from index 0.
  `HourlyUnavailable` means show the placeholder message, not an error.
- The historical-forecast archive has no `total_column_integrated_water_vapour` (live run 2026-11-24), so
  `get_upper` fills it with `derive_pw()` (∫ q dp/g, surface to 200 hPa) and sets `_pw_derived`; screens show "PW*".
- Climate screens take unit labels from `cunits(clim)`, never from the forecast `data`.
- Climate footers: left is `src_note(rows)` (mentions "dashed forecast" only if forecast rows exist); right is
  "reanalysis thru <date>" for today, empty for past dates. Never "updated N min ago" (that's the forecast's clock).

## Settled by the first full live run (2026-11-24, Tucson, 67/67 passed)
- Archive model: `era5_seamless` is accepted, and all six `EXTRA_DAILY` variables return data.
- All three `UPPER_OPTIONAL` hourly names are accepted (TCWV, lifted_index, convective_inhibition).
- The reanalysis runs about 7 days behind (last obs 11-17 on 11-24); the forecast's past_days fills the gap.
- Archive `daily_units` uses "mp/h" and "inch"; `nice_unit()` maps these for display.

## Grid metadata / Site screen
Every Open-Meteo response carries the grid-cell center (`latitude`/`longitude`), the 90 m DEM `elevation` used for
downscaling, and time-zone fields. It carries no cell size or model name. `grid_info(resp)` extracts them. The forecast
`data` and `upper` dicts keep them at top level, and the climatology stores them in `clim["grid"]`
(`ensure_clim_grid` backfills older caches). `geocode()` keeps elevation, timezone and population in the location dict.

## Inspect cursor
Plot screens call `set_probe(times, xfn, y0, y1, lines, anchor, hourly, one_line)` after drawing. `render(inspect=)`
draws the cursor and readout via `draw_probe` and stores the resolved index in `PROBE["index"]`, which App.draw reads back.
`lines(i)` returns (line1, line2) lists of (text, color): line1 replaces the legend row, line2 the footer (None keeps it),
and one_line puts everything in the footer (stacked panels). New plot screens should register a probe.
`test_every_plot_screen_supports_inspect` lists the inspectable screens.

## Keys on the device
Week step: `b`/`n` always work. Whether `-`/`=` arrive as KEY_MINUS/KEY_EQUAL on the TCA8418 keyboard is
unconfirmed (they may be on the sym layer). `--debug-keys` prints every key event, and unmapped keys always go
to app.log.

## Launcher packaging
`launcher/install_launcher.sh` installs into /usr/share/APPLaunch (bin/cz_weather.py, bin/run-cz-weather,
applications/cz-weather.desktop, share/images/cz-weather.png) and restarts the user service
`APPLaunch.service`. The wrapper picks /dev/fb_lcd and logs to ~/.cache/cz-weather/app.log. Apps exit
themselves: `q`, or hold Esc 2 s (`EscHold`, polled via select in `evdev_keys`). SIGTERM exits cleanly and
blanks the screen. Keep the files LF line-endings: they run on Linux.

## Still open (device only)
1. ~~Framebuffer~~: /dev/fb0 (alias /dev/fb_lcd), 320×170, 16 bpp, RGB565 via `pack_rgb565` (Pillow has no packer).
   Colors confirmed correct on the device. The keyboard is evdev "tca8418c"; arrows are fn+F/X/Z/C.
2. Does the launcher release the screen and keyboard to the app cleanly, and redraw on exit?
3. How long does the first climatology build (3 chunks + percentiles) take on the CM0?
4. Is memory OK on 512 MB during the climatology build?

## Key bindings (keep README in sync)
←/→ or `,` `/` move between screens · `d` view date · `-` `=` step the date a week · `1`–`6` jump to a section · ↑/↓ or `;` `.` step time on the Sounding
screen · `r` refresh · `l` set location (name or "lat, lon") · `u` switch units · `q` quit.
In the search box, `;` `.` `,` are typed as text; only real arrow keys move the selection.
