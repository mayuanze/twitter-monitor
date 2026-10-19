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
from tweet_card import expand_urls
from webhook_notifier import WebhookMessage, WebhookNotifier

STATE_PATH = os.path.join(PROJ, 'state.json')
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


def main() -> int:
    username = need('X_USERNAME')
    auth_token = need('X_AUTH_TOKEN')
    ct0 = need('X_CT0')
    webhook = need('WEBHOOK_URL')
    target = os.environ.get('TARGET_USER', '').strip() or 'nikgeneburn'

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
                    url_list=[webhook], text=original_text(tweet, target, tid)))
                print('  original text sent')
                continue

            # Fallback: plain text plus the photos as separate image messages.
            text = expand_urls(tweet, parse_text_from_tweet(tweet) or '')
            photos, videos = parse_media_from_tweet(tweet)

            image_blobs = []
            failed_urls = []
            for url in photos[:MAX_PHOTOS]:
                blob = download_image(url)
                if blob:
                    image_blobs.append(blob)
                else:
                    failed_urls.append(url)

            stamp = beijing_time(tweet)
            head = '[{}]{}'.format(target, ' ' + stamp if stamp else '')
            body = '{}\n{}{}'.format(head, text[:900], '…' if len(text) > 900 else '')
            if failed_urls:
                body += '\n' + '\n'.join(failed_urls)
            if videos:
                body += '\n' + '\n'.join(videos[:2])
            body += '\n\nhttps://x.com/{}/status/{}'.format(target, tid)

            WebhookNotifier.send_message(WebhookMessage(url_list=[webhook], text=body))
            print('pushed {} as text ({} image(s) embedded)'.format(tid, len(image_blobs)))

            for blob in image_blobs:
                if send_wecom_image(webhook, blob):
                    print('  image sent ({} KB)'.format(len(blob) // 1024))
                else:
                    print('  image skipped (not WeCom, or rejected)')

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
