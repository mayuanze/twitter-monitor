"""Import X login cookies from your browser and verify them.

Why this instead of `generate-auth-cookie`:
the username/password flow drives X's onboarding API step by step, which X
changes often and guards against automation. Copying the two cookies your
logged-in browser already holds is both more reliable and avoids handing the
account password to a script.

How to get the two values (Chrome / Edge):
    1. Open https://x.com in the browser and make sure you are logged in
    2. Press F12 -> Application (应用程序) -> Storage -> Cookies -> https://x.com
    3. Find `auth_token` and `ct0`, copy each Value

Usage:
    python cookie_tool.py
"""

import getpass
import json
import os
import sys

# Must happen before requests reads the environment; this host injects its own
# proxy variable that cannot reach X.
_PROXY = os.environ.get('TWITTER_MONITOR_PROXY', 'socks5h://127.0.0.1:10808')
os.environ['HTTP_PROXY'] = _PROXY
os.environ['HTTPS_PROXY'] = _PROXY
os.environ['ALL_PROXY'] = _PROXY
os.environ['NO_PROXY'] = '127.0.0.1,localhost,::1'
os.environ.pop('no_proxy', None)

import requests

BEARER = ('AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33'
          'AGWWjCpTnA')
PROJ = os.path.dirname(os.path.abspath(__file__))
COOKIE_DIR = os.path.join(PROJ, 'cookies')


def verify(auth_token: str, ct0: str) -> bool:
    """Verify through the same GraphQL endpoint the monitor actually uses.

    v1.1/account/verify_credentials rejects these OAuth2 cookie sessions with
    code 215 even when the cookies are perfectly valid, so it is useless here.
    """
    print('  初始化 GraphQL 接口数据...')
    try:
        from graphql_api import GraphqlAPI
        from twitter_watcher import _build_params, _get_auth_headers
        from utils import find_one
    except Exception as e:
        print('  初始化失败: {}: {}'.format(type(e).__name__, str(e)[:200]))
        return False

    url, method, headers, features = GraphqlAPI.get_api_data('UserByScreenName')
    cookies = {'auth_token': auth_token, 'ct0': ct0}
    auth_headers = _get_auth_headers(headers, cookies)
    params = _build_params({'variables': {'screen_name': 'X'}, 'features': features})

    try:
        r = requests.request(method=method, url=url, headers=auth_headers, params=params, timeout=60)
    except Exception as e:
        print('  请求失败: {}: {}'.format(type(e).__name__, str(e)[:200]))
        return False

    print('  GraphQL HTTP {}'.format(r.status_code))
    if r.status_code != 200:
        print('  body: {}'.format(r.text[:300]))
        if r.status_code in (401, 403):
            print('  -> cookie 无效或被 X 拒绝（code 353/32 常见于会话过期）')
        return False

    try:
        data = r.json()
    except ValueError:
        print('  返回非 JSON: {}'.format(r.text[:200]))
        return False

    if 'errors' in data:
        print('  X 返回错误: {}'.format(str(data['errors'])[:300]))
        return False

    rest_id = find_one(data, 'rest_id')
    if not rest_id:
        print('  响应里没有 rest_id，无法判定: {}'.format(str(data)[:300]))
        return False

    print('验证成功 -> 可读取 @X 的数据 (id={})'.format(rest_id))
    return True


def describe(label: str, value: str, normal: str) -> bool:
    """Print a masked summary so the user can spot a wrong copy."""
    head = value[:4]
    tail = value[-4:]
    print('  %-11s 长度=%-4d  %s****%s   参考: %s' % (label, len(value), head, tail, normal))
    problems = []
    if '=' in value or ';' in value:
        problems.append('包含 = 或 ;，可能把 "name=" 前缀一起粘进来了')
    if any(c.isspace() for c in value):
        problems.append('包含空格或换行')
    if not value.isascii():
        problems.append('包含非 ASCII 字符')
    if value[:1] in ('"', "'") or value[-1:] in ('"', "'"):
        problems.append('首尾带引号，请只复制引号内的值')
    for p in problems:
        print('      ! {}'.format(p))
    return not problems


def main() -> None:
    print('=== 从浏览器导入 X 登录 cookie ===')
    username = input('X 用户名（不含 @，作为本地文件名）: ').strip().lstrip('@')
    if not username:
        sys.exit('用户名不能为空')

    auth_token = getpass.getpass('auth_token（粘贴后回车，输入不回显）: ').strip()
    ct0 = getpass.getpass('ct0（粘贴后回车，输入不回显）: ').strip()
    if not auth_token or not ct0:
        sys.exit('auth_token 和 ct0 都不能为空')

    print('\n--- 输入核对 ---')
    ok_a = describe('auth_token', auth_token, '40 位十六进制')
    ok_c = describe('ct0', ct0, '常见 32 或 160 位')
    if len(auth_token) != 40:
        print('  ! auth_token 通常正好 40 位，当前不是，很可能复制不全')
    if ok_a is False or ok_c is False:
        sys.exit('\n输入格式有问题，请重新复制。在 DevTools 里双击 Value 单元格全选再复制。')

    print('\n正在验证...')
    if not verify(auth_token, ct0):
        sys.exit('\n验证未通过，文件未写入。')

    os.makedirs(COOKIE_DIR, exist_ok=True)
    path = os.path.join(COOKIE_DIR, '{}.json'.format(username))
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'auth_token': auth_token, 'ct0': ct0}, f, indent=2)
    print('\n已写入: {}'.format(path))
    print('下一步: 把 "{}" 填进 config/token.json 的 twitter_auth_username_list'.format(username))


if __name__ == '__main__':
    main()
