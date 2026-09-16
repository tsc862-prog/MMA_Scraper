"""
Refresh the app's precomputed views.

The Rewatch app reads two materialized views instead of re-running the
expensive joins on every page load:

  event_flags_mv        per-event has_results / has_video flags  (event list)
  community_fight_base  per-fight base rows for the community dashboard

They only change when the scrapers write, so this script is run at the END of
every scraper pass -- the last step of the nightly GitHub Actions workflow and
each pass of the live-event loop (.claude/skills/current-event). Until it runs,
the events list and community stats reflect the previous pass; the event card
itself always reads live data.

Calls the refresh_app_caches() RPC (service_role only; refreshes both views
CONCURRENTLY so app readers never block). Takes a few seconds.

Usage:
    python refresh_app_caches.py

.env needs SUPABASE_URL and SUPABASE_KEY (service role).
"""

import os
import sys
import time
import requests

# Load .env (same minimal parser as the scrapers -- no python-dotenv dependency)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())

SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')


def refresh_app_caches(retries=3):
    """POST to the refresh RPC. Returns True on success. Retries on transient
    failures (the nightly job is hours long; a single blip shouldn't leave the
    app a day stale)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError('Missing SUPABASE_URL or SUPABASE_KEY in .env')
    headers = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
    }
    last_err = None
    for attempt in range(1, retries + 1):
        t0 = time.time()
        try:
            resp = requests.post(f'{SUPABASE_URL}/rest/v1/rpc/refresh_app_caches',
                                 headers=headers, json={}, timeout=300)
            if resp.ok:
                print(f'App caches refreshed in {time.time() - t0:.1f}s '
                      f'(event_flags_mv, community_fight_base).')
                return True
            last_err = f'HTTP {resp.status_code}: {resp.text[:300]}'
        except requests.RequestException as e:
            last_err = str(e)
        print(f'Refresh attempt {attempt} failed: {last_err}')
        if attempt < retries:
            time.sleep(10 * attempt)
    return False


if __name__ == '__main__':
    ok = refresh_app_caches()
    sys.exit(0 if ok else 1)
