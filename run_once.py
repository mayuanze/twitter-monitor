"""Single-shot poller for GitHub Actions.

The resident version (`main.py run`) keeps state in memory and polls every
15 seconds. Actions has no long-lived process, so this script does exactly one
poll and exits, persisting the high-water mark in `state.json` for the next run.

Environment variables (set from repository secrets):
    X_USERNAME    local label for the cookie file
    X_AUTH_TOKEN  auth_token cookie from the browser
    X_CT0         ct0 cookie from the browser
    WEBHOOK_URL   WeCom / DingTalk / Feishu / PushPlus webhook
    TARGET_USER   X handle to watch (default: nikgeneburn)
    STATE_FILE    high-water mark file (default: state.json); one per target

First run only records a baseline: it will not flood the channel with history.

Images are downloaded on the runner and re-uploaded as base64 `image`
messages, because pbs.twimg.com is unreachable from mainland China -- posting
the raw URL would produce a link nobody can open.
"""

import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

import tweet_card
from twitter_watcher import TwitterWatcher
from utils import (find_all, find_one, parse_create_time_from_tweet,
                   parse_media_from_tweet, parse_text_from_tweet)
from qq_notifier import QQGroupNotifier
from tweet_card import expand_urls
from webhook_notifier import WebhookMessage, WebhookNotifier

STATE_PATH = os.path.join(PROJ, os.environ.get('STATE_FILE', '').strip() or 'state.json')
COOKIE_DIR = os.path.join(PROJ, 'cookies')

# WeCom group bot: image messages take raw bytes <= 2MB, JPG/PNG only.
WECOM_MAX_IMAGE = 2 * 1024 * 1024
MAX_PHOTOS = 4


def need(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        sys.exit('missing environment variable: {}'.format(name))
    return value


def download_image(url: str) -> bytes | None:
    """Fetch a tweet photo. Falls back to the smaller rendition if oversized."""
    for candidate in (url, url.split('?')[0] + '?name=small'):
        try:
            response = requests.get(candidate, timeout=30)
        except Exception as e:
            print('image download failed {}: {}'.format(candidate, e))
            continue
        if response.status_code != 200 or not response.content:
            print('image download {} -> HTTP {}'.format(candidate, response.status_code))
            continue
        if len(response.content) <= WECOM_MAX_IMAGE:
            return response.content
        print('image too large ({} bytes), retrying smaller rendition'.format(len(response.content)))
    return None


def send_wecom_image(webhook: str, data: bytes) -> bool:
    """Push one image as a native WeCom image message (no external URL needed)."""
    if 'qyapi.weixin.qq.com' not in webhook:
        return False
    if not data or len(data) > WECOM_MAX_IMAGE:
        return False
    payload = {
        'msgtype': 'image',
        'image': {
            'base64': base64.b64encode(data).decode(),
            'md5': hashlib.md5(data).hexdigest(),
        },
    }
    # qyapi.weixin.qq.com must be reached directly: a proxy would break it.
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.post(webhook, json=payload, timeout=60)
        info = response.json()
    except Exception as e:
        print('image push failed: {}'.format(e))
        return False
    if info.get('errcode') not in (None, 0):
        print('image rejected: {}'.format(info))
        return False
    return True


def beijing_time(tweet: dict) -> str:
    try:
        created = parse_create_time_from_tweet(tweet)
        return created.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return ''


def _fit_bytes(text: str, limit: int = 1900) -> str:
    """WeCom caps text messages at 2048 bytes of UTF-8, not 2048 characters."""
    data = text.encode('utf-8')
    if len(data) <= limit:
        return text
    return data[:limit - 12].decode('utf-8', 'ignore') + '\n…(截断)'


def original_text(tweet: dict, target: str, tid: str) -> str:
    """Plain-text copy of the tweet, so the reader can long-press and copy it."""
    stamp = beijing_time(tweet)
    lines = ['原文 @{}{}'.format(target, ' · ' + stamp if stamp else ''), '']

    body = expand_urls(tweet, parse_text_from_tweet(tweet) or '').strip()
    lines.append(body or '（此推文没有正文）')

    quoted = find_one(tweet, 'quoted_status_result')
    if isinstance(quoted, dict):
        quoted = quoted.get('result') or quoted
        if isinstance(quoted, dict):
            author = find_one(quoted, 'user_results')
            core = find_one(author, 'core') or {}
            qhandle = core.get('screen_name') or ''
            qtext = expand_urls(quoted, parse_text_from_tweet(quoted) or '').strip()
            if qtext:
                lines += ['', '【引用 @{}】'.format(qhandle), qtext]

    lines += ['', 'https://x.com/{}/status/{}'.format(target, tid)]
    return _fit_bytes('\n'.join(lines))


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    return cjk / len(text)


BROWSER_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}


def _google_translate(text: str, lang: str) -> str:
    """Google web endpoint. Sends a browser UA: without it Google answers with
    an HTML interstitial and the JSON parse fails."""
    for host in ('translate.googleapis.com', 'translate.google.com'):
        try:
            response = requests.get(
                'https://{}/translate_a/single'.format(host),
                params={'client': 'gtx', 'sl': 'auto', 'tl': lang, 'dt': 't',
                        'q': text[:1800]},
                headers=BROWSER_HEADERS, timeout=30)
        except Exception as e:
            print('  google({}) request failed: {}'.format(host, e))
            continue
        if response.status_code != 200:
            print('  google({}) HTTP {}'.format(host, response.status_code))
            continue
        try:
            data = response.json()
            return ''.join(part[0] for part in data[0] if part and part[0]).strip()
        except Exception:
            print('  google({}) non-JSON reply: {}'.format(
                host, response.text[:100].replace('\n', ' ')))
    return ''


def _mymemory_translate(text: str, lang: str) -> str:
    """MyMemory free tier: 500-byte query limit, so send it in chunks."""
    pieces = []
    for start in range(0, len(text), 400):
        chunk = text[start:start + 400]
        try:
            response = requests.get(
                'https://api.mymemory.translated.net/get',
                params={'q': chunk, 'langpair': 'en|' + lang},
                headers=BROWSER_HEADERS, timeout=30)
            payload = response.json()
        except Exception as e:
            print('  mymemory failed: {}'.format(e))
            return ''
        translated = (payload.get('responseData') or {}).get('translatedText') or ''
        if not translated or str(payload.get('responseStatus')) != '200':
            print('  mymemory rejected: {}'.format(str(payload)[:120]))
            return ''
        pieces.append(translated)
    return ''.join(pieces).strip()


def _lingva_translate(text: str, lang: str) -> str:
    """Lingva Translate: open-source Google front-end, no key required."""
    for host in ('lingva.ml', 'lingva.garudalinux.org'):
        url = 'https://{}/api/v1/auto/{}/{}'.format(
            host, lang, requests.utils.quote(text[:1500], safe=''))
        try:
            response = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        except Exception as e:
            print('  lingva({}) failed: {}'.format(host, e))
            continue
        if response.status_code != 200:
            print('  lingva({}) HTTP {}'.format(host, response.status_code))
            continue
        try:
            return (response.json().get('translation') or '').strip()
        except Exception:
            print('  lingva({}) non-JSON reply'.format(host))
            continue
    return ''


def translate(text: str, lang: str = 'zh-CN') -> str:
    """Translate with free endpoints, trying several providers in turn."""
    if not text.strip():
        return ''
    if _cjk_ratio(text) > 0.3:
        return ''  # already Chinese, nothing to do

    for name, handler in (('google', _google_translate),
                          ('mymemory', _mymemory_translate),
                          ('lingva', _lingva_translate)):
        result = handler(text, lang)
        if result:
            print('  translated via {}'.format(name))
            return result
    return ''


def translated_text(tweet: dict, target: str, tid: str) -> str:
    """Chinese rendering of the tweet. Empty string means 'do not send one'."""
    original = expand_urls(tweet, parse_text_from_tweet(tweet) or '').strip()
    body = translate(original)
    if not body:
        return ''
    if body.strip() == original.strip():
        return ''  # a bare link or untranslatable text: a duplicate adds nothing

    stamp = beijing_time(tweet)
    lines = ['译文 @{}{}'.format(target, ' · ' + stamp if stamp else ''), '', body]

    quoted = find_one(tweet, 'quoted_status_result')
    if isinstance(quoted, dict):
        quoted = quoted.get('result') or quoted
        if isinstance(quoted, dict):
            author = find_one(quoted, 'user_results')
            core = find_one(author, 'core') or {}
            qhandle = core.get('screen_name') or ''
            qtext = translate(expand_urls(quoted, parse_text_from_tweet(quoted) or '').strip())
            if qtext:
                lines += ['', '【引用 @{}】'.format(qhandle), qtext]

    lines += ['', 'https://x.com/{}/status/{}'.format(target, tid)]
    return _fit_bytes('\n'.join(lines))


def main() -> int:
    username = need('X_USERNAME')
    auth_token = need('X_AUTH_TOKEN')
    ct0 = need('X_CT0')
    webhook = need('WEBHOOK_URL')
    target = os.environ.get('TARGET_USER', '').strip() or 'nikgeneburn'

    # QQ group is optional: skip silently when any of the three values is absent.
    qq_app_id = os.environ.get('QQ_APP_ID', '').strip()
    qq_app_secret = os.environ.get('QQ_APP_SECRET', '').strip()
    qq_group = os.environ.get('QQ_GROUP_OPENID', '').strip()
    qq_sender = None
    if qq_app_id and qq_app_secret and qq_group:
        qq_sender = QQGroupNotifier(qq_app_id, qq_app_secret, qq_group)
    else:
        print('qq group disabled: QQ_APP_ID / QQ_APP_SECRET / QQ_GROUP_OPENID not all set')

    os.makedirs(COOKIE_DIR, exist_ok=True)
    with open(os.path.join(COOKIE_DIR, '{}.json'.format(username)), 'w', encoding='utf-8') as f:
        json.dump({'auth_token': auth_token, 'ct0': ct0}, f)

    first_run = not os.path.exists(STATE_PATH)
    last_id = 0
    if not first_run:
        try:
            with open(STATE_PATH, 'r', encoding='utf-8') as f:
                last_id = int(json.load(f).get('last_id') or 0)
        except Exception as e:
            print('state unreadable, fall back to baseline: {}'.format(e))
            first_run = True

    watcher = TwitterWatcher([username], COOKIE_DIR)
    user_id = watcher.get_id_by_username(target)
    if not user_id:
        print('cannot resolve @{}'.format(target))
        return 1
    print('target=@{} user_id={}'.format(target, user_id))

    response = watcher.query('UserTweetsAndReplies', {
        'userId': user_id,
        'includePromotedContent': True,
        'withVoice': True,
        'count': 40,
    })
    if response is None:
        print('query failed: all tokens unavailable')
        return 1

    tweets = find_all(response, 'tweet_results') or []
    fresh = []
    max_id = last_id
    for tweet in tweets:
        author = find_one(tweet, 'user_results')
        if find_one(author, 'rest_id') != user_id:
            continue  # skip promoted / other people's tweets in the timeline
        rest_id = find_one(tweet, 'rest_id')
        if not rest_id:
            continue
        tid = int(rest_id)
        max_id = max(max_id, tid)
        if tid > last_id:
            fresh.append((tid, tweet))

    fresh.sort(key=lambda item: item[0])
    print('first_run={} last_id={} max_id={} new={}'.format(first_run, last_id, max_id, len(fresh)))

    if first_run:
        print('baseline recorded, nothing pushed this run')
    elif fresh:
        WebhookNotifier.init(logger_name='webhook')
        for tid, tweet in fresh:
            text_body = original_text(tweet, target, tid)
            translation = translated_text(tweet, target, tid)

            # Preferred output: one screenshot-style card image that already
            # contains avatar, text, media, quoted tweet and engagement counts.
            card = None
            try:
                card = tweet_card.render_card(tweet, target)
            except Exception as e:
                print('card render failed for {}: {}'.format(tid, e))

            if card and send_wecom_image(webhook, card):
                print('pushed {} as card ({} KB)'.format(tid, len(card) // 1024))
                # Also send the plain text so it can be copied on a phone.
                WebhookNotifier.send_message(WebhookMessage(
                    url_list=[webhook], text=text_body))
                print('  original text sent')
                if translation:
                    WebhookNotifier.send_message(WebhookMessage(
                        url_list=[webhook], text=translation))
                    print('  translation sent')
                else:
                    print('  translation skipped (already Chinese, unchanged, or failed)')
            else:
                # Fallback: plain text plus the photos as separate image messages.
                photos, videos = parse_media_from_tweet(tweet)
                image_blobs = []
                failed_urls = []
                for url in photos[:MAX_PHOTOS]:
                    blob = download_image(url)
                    if blob:
                        image_blobs.append(blob)
                    else:
                        failed_urls.append(url)

                body = text_body
                if failed_urls:
                    body += '\n' + '\n'.join(failed_urls)
                if videos:
                    body += '\n' + '\n'.join(videos[:2])

                WebhookNotifier.send_message(WebhookMessage(url_list=[webhook], text=body))
                print('pushed {} as text ({} image(s) embedded)'.format(tid, len(image_blobs)))

                for blob in image_blobs:
                    if send_wecom_image(webhook, blob):
                        print('  image sent ({} KB)'.format(len(blob) // 1024))
                    else:
                        print('  image skipped (not WeCom, or rejected)')

            # QQ group gets text only: rich media would need the PNG uploaded
            # to QQ's file API first, and the card has no public URL.
            if qq_sender is not None:
                if qq_sender.send(text_body):
                    print('  qq group: original sent')
                else:
                    print('  qq group: original failed')
                if translation and qq_sender.send(translation):
                    print('  qq group: translation sent')

    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump({
            'last_id': max_id,
            'target': target,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }, f, indent=2)
    print('state saved: last_id={}'.format(max_id))
    return 0


if __name__ == '__main__':
    sys.exit(main())
