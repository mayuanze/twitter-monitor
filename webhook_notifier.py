"""Generic webhook notifier.

Supports the common China-accessible push channels out of the box by sniffing
the webhook host:

  * WeCom (企业微信) group bot   -> https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...
  * DingTalk (钉钉) group bot     -> https://oapi.dingtalk.com/robot/send?access_token=...
  * Feishu (飞书) group bot       -> https://open.feishu.cn/open-apis/bot/v2/hook/...
  * PushPlus (微信推送)           -> https://www.pushplus.plus/send

Anything else falls back to a plain {"text": ...} JSON POST, which matches
most self-hosted bridges.
"""

import logging

import requests
from urllib.parse import parse_qs, urlparse

from notifier_base import Message, NotifierBase

# Push channels usually cap text at 2048 bytes / 4096 chars. Keep a margin.
MAX_TEXT_LENGTH = 1800


def _truncate(text: str, limit: int = MAX_TEXT_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 20] + '\n...(truncated)'


# Push channels that live inside China must bypass the proxy entirely: the
# tunnel exits abroad and the connection to these hosts simply fails.
DOMESTIC_HOSTS = ('qyapi.weixin.qq.com', 'oapi.dingtalk.com', 'open.feishu.cn', 'pushplus')


class WebhookMessage(Message):

    def __init__(self,
                 url_list: list[str],
                 text: str,
                 photo_url_list: list[str] | None = None,
                 video_url_list: list[str] | None = None) -> None:
        super().__init__(text, photo_url_list, video_url_list)
        self.url_list = url_list


class WebhookNotifier(NotifierBase):
    notifier_name = 'Webhook'

    @classmethod
    def init(cls, logger_name: str) -> None:
        cls.logger = logging.getLogger('{}'.format(logger_name))
        cls.logger.info('Init webhook notifier succeed.')
        super().init()

    @classmethod
    def _build_payload(cls, url: str, text: str) -> dict:
        text = _truncate(text)
        if 'qyapi.weixin.qq.com' in url:
            # WeCom group bot
            return {'msgtype': 'text', 'text': {'content': text}}
        if 'oapi.dingtalk.com' in url:
            # DingTalk group bot
            return {'msgtype': 'text', 'text': {'content': text}}
        if 'open.feishu.cn' in url:
            # Feishu group bot
            return {'msg_type': 'text', 'content': {'text': text}}
        if 'pushplus' in url:
            # PushPlus -> delivers into WeChat via official account.
            # The token must be carried in the body; accept it from the query string.
            query = parse_qs(urlparse(url).query)
            return {
                'token': query.get('token', [''])[0],
                'title': 'twitter-monitor',
                'content': text,
                'template': 'txt',
            }
        return {'text': text}

    @classmethod
    def _post(cls, url: str, payload: dict) -> None:
        # Domestic hosts must bypass the proxy: the tunnel exits abroad and the
        # connection to them fails. Note that proxies={} is NOT enough here --
        # requests merges the environment proxies back in via setdefault unless
        # trust_env is disabled.
        if any(h in url for h in DOMESTIC_HOSTS):
            session = requests.Session()
            session.trust_env = False
            response = session.post(url, json=payload, timeout=60)
        else:
            response = requests.post(url, json=payload, timeout=60)
        if response.status_code != 200:
            raise RuntimeError('Post request error: {}, {}\nurl: {}'.format(
                response.status_code, response.text, url))
        # WeCom / DingTalk report failures inside a 200 response.
        try:
            data = response.json()
        except ValueError:
            return
        errcode = data.get('errcode')
        if errcode not in (None, 0):
            raise RuntimeError('Webhook returned errcode {}: {}\nurl: {}'.format(
                errcode, data.get('errmsg'), url))

    @classmethod
    def send_message(cls, message: WebhookMessage) -> None:
        assert cls.initialized
        assert isinstance(message, WebhookMessage)
        text = message.text
        if message.photo_url_list:
            text += '\n' + '\n'.join(message.photo_url_list)
        if message.video_url_list:
            text += '\n' + '\n'.join(message.video_url_list)
        for url in message.url_list:
            cls._post(url, cls._build_payload(url, text))
