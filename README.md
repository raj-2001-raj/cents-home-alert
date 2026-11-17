# CENT@HOME / CENT@CASA seat alert

Watches the CISIA CEnT-S calendar (English test) and alerts you by email and phone push
the moment a CENT@HOME (= CENT@CASA) session has seats, or a new date is published.
CENT@UNI is ignored. Everything here is free: no paid API, no AI model, no server to rent.

Calendar watched: https://testcisia.it/calendario.php?tolc=cents&l=gb&lingua=inglese

## Runners

| Runner | Works when your computer is off | Checks | Alerts |
|---|---|---|---|
| **Google Apps Script** (main) | Yes | every 1 min | Gmail (Google's servers can't reach ntfy, so no push) |
| GitHub Actions (backup) | Yes | every ~5 min, can start late | phone push |
| Mac (`install_mac.sh`) | No, only while awake | every 1 min | phone push + Mac popup, voice, opens booking page |

Each alert is tagged `[Google]`, `[GitHub]` or `[Mac]`, so with several runners you may get
the same alert more than once. That's deliberate redundancy.

## What you get notified about

| Event | Alert |
|---|---|
| Seats open on a CENT@HOME date (new release, or freed by a cancellation) | **BOOK NOW** email + urgent push. Repeated every 10 min (max 3) while seats stay open |
| New CENT@HOME date appears, not bookable yet | Email + high-priority push |
| A CENT@HOME date shows a status the script doesn't recognise | Email + high-priority push |
| Calendar unreadable for several checks in a row | Warning, so the monitor never fails silently |
| Every morning at 09:00 Italy time | "Still running" email (Google runner) |

Bookings close at 14:00 Italy time on the deadline day.

## 1. Google Apps Script (main runner, 2 minutes)

`google_apps_script.gs` (kept out of git: it contains your email addresses).

1. Open https://script.google.com → **New project**.
2. Delete the sample code, paste the whole file, click **Save**.
3. Choose **setup** in the function dropdown → **Run**.
4. Allow the permissions (Advanced → "Go to … (unsafe)" → Allow; it's your own script).
5. A "monitor started" email arrives. The execution log shows the current calendar.

To stop: run `stopMonitor`. To test alerts: run `sendTestAlert`.
Free Google accounts allow 90 min of script time a day; one check takes ~1-2 s, so
checking every minute uses roughly 30-45 min.

## 2. Phone push (ntfy, free, no account)

Install **ntfy** (Play Store / App Store), tap **+**, subscribe to the topic in
`config.json` → `ntfy_topic` (server `ntfy.sh`). On Android set the subscription to
max priority. Keep the topic private: anyone who knows it can read the alerts.

## 3. GitHub Actions (backup runner)

1. Push this folder to a **public** repo (unlimited free minutes).
2. Settings → Secrets and variables → Actions → add `NTFY_TOPIC`.
3. Actions → **CENT@HOME monitor** → **Run workflow** once.

This runner sends phone pushes only; emails come from the Google runner.

## 4. Mac runner

```bash
./install_mac.sh                 # install / apply config.json changes
python3 cents_monitor.py --test  # test alert to every channel
python3 cents_monitor.py --show  # print the calendar right now
tail -f ~/Library/Application\ Support/cents-alert/monitor.log
./uninstall_mac.sh               # stop and remove
```

## Files

- `cents_monitor.py`: the checker for Mac and GitHub (Python standard library only)
- `google_apps_script.gs`: the same checker for Google Apps Script
- `config.json`: your Mac alert settings (private, gitignored)
- `.github/workflows/monitor.yml`: GitHub Actions runner
- `state.json`: what was already alerted (created automatically)
