# Cincinnati Metro 3X bus alert

Sends a push notification to your iPhone when route 3X is about
(walk time + buffer) minutes from Montgomery Rd & Kemper Rd.

It uses Metro's live arrival estimates for your stop, so the alert time
accounts for traffic and delays instead of just distance.

## Setup (about 10 minutes, on a computer)

1. **Phone:** install the free **ntfy** app from the App Store. Tap +,
   subscribe to a topic with a long random name, for example
   `kingsley-bus-8f3k2x9q` (anyone who knows the name can send you pushes,
   so make it unguessable).
2. **GitHub:** create a free account, then a **new public repository** and
   upload all files from this folder (keep the `.github/workflows` path).
   The repository is public so Actions minutes are free; nothing secret is
   in the files.
3. In the repo go to **Settings > Secrets and variables > Actions > New
   repository secret**. Name: `NTFY_TOPIC`, value: your topic name.
4. Go to the **Actions** tab, enable workflows, open **bus-alert** and click
   **Run workflow**. That runs it once immediately (it ignores the
   time-of-day check).

## Weather note

When the watcher wakes up it also checks today's hourly forecast for
Montgomery, OH (free Open-Meteo data, no key needed) from now until 6 pm,
which covers your ride home:

- Snow expected: "Wear your snow jacket"
- Rain likely (40%+ chance or measurable rain): "Take an umbrella"
- Otherwise it stays quiet. Set `WEATHER_ALWAYS: "1"` if you also want a
  daily "dry today" note, so you know the check ran.

Test it any time with `python bus_alert.py --weather-only`. A weather
problem never blocks the bus alert. Tunable settings: `RAIN_PROB`,
`SNOW_MIN_IN`, `WEATHER_UNTIL`, `LAT`, `LON`, `NO_WEATHER`.

## Check it works

On any computer with Python 3.9+:

    pip install -r requirements.txt
    export NTFY_TOPIC=your-topic-name
    python bus_alert.py --test-notify    # push should arrive on your phone
    python bus_alert.py --find-stop      # confirm it picked the right stop_id

If `--find-stop` picks the wrong side of the road, copy the right
`stop_id` and add `STOP_ID: "12345"` to the `env:` block in the workflow.

## Settings (in the workflow file)

| Setting | Meaning |
|---|---|
| WALK_MIN / BUFFER_MIN | Alert fires when the bus is this many minutes (sum) from your stop |
| BUS_FROM / BUS_TO | Only buses scheduled at your stop in this window trigger an alert |
| START_LOCAL | When the watcher wakes up (also shift the two cron lines) |
| MAX_ALERTS | Alerts per morning (default 1) |

## Notes

- Runs Monday to Friday only (3X is weekday peak service).
- GitHub scheduled runs can start a few minutes late, so START_LOCAL should
  be at least 20 to 30 minutes before the earliest bus you might catch.
- iOS Focus / Do Not Disturb can silence ntfy. In iOS Settings, allow
  ntfy under your Focus. For alerts that break through, use the Pushover
  app instead and add `PUSHOVER_TOKEN` and `PUSHOVER_USER` as secrets.
