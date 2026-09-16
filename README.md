# HPInstantOnAPI
This is a code base that will connect to the HP Instant on networking. 

`instanton.py` pulls site, device, client, alert and network data from the
undocumented HPE Aruba Networking Instant On cloud API (the one the web portal
at portal.instant-on.hpe.com uses). It is not supported by HPE and may break
without notice.

## Setup
Requires Python 3.10+ and `requests`.

```bash
cp .env.example .env   # fill in INSTANTON_USER / INSTANTON_PASS
```

Use a dedicated read-only portal account **without MFA** — the scripted login
cannot answer an MFA challenge.

## Usage
```bash
./instanton.py                         # dump raw JSON to output/<timestamp>/ and print a summary
./instanton.py --json                  # summary as JSON (for monitoring tools)
./instanton.py --summary-only          # summary only, no files written
./instanton.py --endpoint inventory    # dump one endpoint (optionally --site <id>)
```

Exit code is 1 when any device is not up or there are active alerts, so it can
be used from cron or Nagios-style checks.
