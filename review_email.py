#!/usr/bin/env python3
"""
review_email.py
Emails a digest of pending review_queue items via Gmail SMTP. Each item has
clickable answer links that hit the `resolve-review` Supabase Edge Function,
so a fight can be resolved straight from the inbox.

  python review_email.py            # send only if there are pending items
  python review_email.py --force    # send even when the queue is empty

.env needs: SUPABASE_URL, SUPABASE_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD
optional:   REVIEW_EMAIL_TO   (recipient; defaults to GMAIL_ADDRESS)
"""

import os
import sys
import ssl
import smtplib
import argparse
import requests
from email.message import EmailMessage

_env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip())

SUPABASE_URL  = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY  = os.environ.get('SUPABASE_KEY', '')
GMAIL_ADDRESS = os.environ.get('GMAIL_ADDRESS', '')
GMAIL_APP_PW  = os.environ.get('GMAIL_APP_PASSWORD', '')
EMAIL_TO      = os.environ.get('REVIEW_EMAIL_TO', '') or GMAIL_ADDRESS

RESOLVE_FN = f'{SUPABASE_URL}/functions/v1/resolve-review'

def _sb_headers():
    return {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'}

def fetch_pending():
    resp = requests.get(
        f'{SUPABASE_URL}/rest/v1/review_queue'
        f'?status=eq.pending&type=eq.gender'
        f'&select=id,token,payload&order=created_at',
        headers=_sb_headers())
    resp.raise_for_status()
    return resp.json()

# ── Email ─────────────────────────────────────────────────────────────────────

def build_html(items):
    rows = []
    for it in items:
        p   = it.get('payload', {}) or {}
        iid = it['id']
        tok = it['token']
        m_link = f'{RESOLVE_FN}?id={iid}&gender=M&token={tok}'
        f_link = f'{RESOLVE_FN}?id={iid}&gender=F&token={tok}'
        btn = ('display:inline-block;padding:10px 22px;margin:4px;border-radius:6px;'
               'text-decoration:none;font-weight:600')
        rows.append(f"""
        <div style="border:1px solid #ddd;border-radius:8px;padding:16px;margin:12px 0">
          <div style="color:#666;font-size:13px">{p.get('event_name','')}</div>
          <div style="font-size:17px;font-weight:600;margin:4px 0 12px">
            {p.get('fighter1_name','?')} &nbsp;vs&nbsp; {p.get('fighter2_name','?')}
          </div>
          <a href="{m_link}" style="{btn};background:#2d6cdf;color:#fff">Men's</a>
          <a href="{f_link}" style="{btn};background:#d6336c;color:#fff">Women's</a>
        </div>""")
    return f"""<!doctype html>
    <div style="font-family:system-ui,sans-serif;max-width:560px;margin:0 auto">
      <h2>MMA gender review</h2>
      <p style="color:#555">{len(items)} fight(s) couldn't be auto-classified.
         Tap the correct division — it saves instantly and propagates to the
         rest of the card.</p>
      {''.join(rows)}
    </div>"""

def send_email(html, count):
    msg = EmailMessage()
    msg['Subject'] = f'MMA review — {count} fight(s) need a gender'
    msg['From']    = GMAIL_ADDRESS
    msg['To']      = EMAIL_TO
    msg.set_content(f'{count} fight(s) need gender review. '
                    f'Open this email in an HTML client to answer.')
    msg.add_alternative(html, subtype='html')

    ctx = ssl.create_default_context()
    with smtplib.SMTP('smtp.gmail.com', 587) as s:
        s.starttls(context=ctx)
        s.login(GMAIL_ADDRESS, GMAIL_APP_PW)
        s.send_message(msg)

# ── Main ──────────────────────────────────────────────────────────────────────

def main(force=False):
    for var in ('SUPABASE_URL', 'SUPABASE_KEY', 'GMAIL_ADDRESS', 'GMAIL_APP_PASSWORD'):
        if not os.environ.get(var):
            print(f'Missing {var} in .env')
            sys.exit(1)

    items = fetch_pending()
    print(f'{len(items)} pending review item(s)')
    if not items and not force:
        print('Nothing to send.')
        return

    send_email(build_html(items), len(items))
    print(f'Sent digest to {EMAIL_TO}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true',
                        help='Send even if the queue is empty')
    args = parser.parse_args()
    main(force=args.force)
