"""Push messages into a QQ group through the official QQ Bot API.

Only text is supported here on purpose: rich media needs the image uploaded to
QQ's file API first, and the rendered card is a local PNG with no public URL.

Environment variables used by run_once.py:
    QQ_APP_ID       bot AppID from q.qq.com
    QQ_APP_SECRET   bot AppSecret
    QQ_GROUP_OPENID group identifier captured by qq_capture.py
"""

import time

import requests

TOKEN_URL = 'https://bots.qq.com/app/getAppAccessToken'
API_BASE = 'https://api.sgroup.qq.com'
MAX_TEXT = 1500


class QQGroupNotifier:
    def __init__(self, app_id: str, app_secret: str, group_openid: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.group_openid = group_openid
        self._token = ''
        self._expires_at = 0.0

    def _refresh_token(self) -> str:
        response = requests.post(
            TOKEN_URL,
            json={'appId': self.app_id, 'clientSecret': self.app_secret},
            timeout=20)
        payload = response.json()
        token = payload.get('access_token') or ''
        if not token:
            raise RuntimeError('getAppAccessToken failed: {}'.format(payload))
        self._token = token
        # refresh a minute early so a slow run never sends an expired token
        self._expires_at = time.time() + int(payload.get('expires_in', 7200)) - 60
        return token

    def _current_token(self) -> str:
        if not self._token or time.time() >= self._expires_at:
            return self._refresh_token()
        return self._token

    def send(self, text: str) -> bool:
        """Send one plain-text message. Returns True when QQ accepted it."""
        content = (text or '').strip()
        if not content:
            return False
        if len(content) > MAX_TEXT:
            content = content[:MAX_TEXT - 12] + '\n…(截断)'

        url = '{}/v2/groups/{}/messages'.format(API_BASE, self.group_openid)
        for attempt in (1, 2):
            headers = {
                'Authorization': 'QQBot {}'.format(self._current_token()),
                'Content-Type': 'application/json',
            }
            # No msg_id on purpose: that makes it a proactive message.
            payload = {'content': content, 'msg_type': 0}
            try:
                response = requests.post(url, json=payload, headers=headers, timeout=20)
            except Exception as e:
                print('  qq send failed: {}'.format(e))
                return False
            if response.status_code in (200, 201, 202):
                return True
            if response.status_code == 401 and attempt == 1:
                print('  qq token rejected, refreshing')
                self._refresh_token()
                continue
            print('  qq rejected HTTP {}: {}'.format(
                response.status_code, response.text[:200]))
            return False
        return False
