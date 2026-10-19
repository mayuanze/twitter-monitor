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
"""

import json
import os
import sys
from datetime import datetime, timezone

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

from twitter_watcher import TwitterWatcher
from utils import find_all, find_one, parse_media_from_tweet, parse_text_from_tweet
from webhook_notifier import WebhookMessage, WebhookNotifier

STATE_PATH = os.path.join(PROJ, 'state.json')
COOKIE_DIR = os.path.join(PROJ, 'cookies')


def need(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        sys.exit('missing environment variable: {}'.format(name))
    return value


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
            text = parse_text_from_tweet(tweet) or ''
            photos, videos = parse_media_from_tweet(tweet)
            body = '[{}] {}{}'.format(target, text[:900], '…' if len(text) > 900 else '')
            if photos:
                body += '\n' + '\n'.join(photos[:4])
            if videos:
                body += '\n' + '\n'.join(videos[:2])
            body += '\n\nLink: https://x.com/{}/status/{}'.format(target, tid)
            WebhookNotifier.send_message(WebhookMessage(url_list=[webhook], text=body))
            print('pushed {}'.format(tid))

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
