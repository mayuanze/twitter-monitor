"""Launcher for twitter-monitor.

Wraps `main.py` and pins the proxy to the local v2rayN socks5 inbound.

Why socks5h and not socks5: the `h` makes the *hostname* resolve on the proxy
side, so DNS lookups happen out of the tunnel. Local DNS here is poisoned
(api.x.com resolved to a Facebook-owned address), so plain socks5 would still
fail even with the tunnel up.

Usage:
    python run.py run
    python run.py check-tokens
    python run.py generate-auth-cookie --username xxx --password yyy

Override the proxy without editing this file:
    set TWITTER_MONITOR_PROXY=http://127.0.0.1:10809
"""

import os
import subprocess
import sys

DEFAULT_PROXY = 'socks5h://127.0.0.1:10808'

proxy = os.environ.get('TWITTER_MONITOR_PROXY', DEFAULT_PROXY)
os.environ['HTTP_PROXY'] = proxy
os.environ['HTTPS_PROXY'] = proxy
os.environ['ALL_PROXY'] = proxy
# Keep loopback traffic off the tunnel; the sandbox-injected proxy is unusable.
os.environ['NO_PROXY'] = '127.0.0.1,localhost,::1'
os.environ.pop('no_proxy', None)

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, 'main.py')

print('[launcher] proxy = {}'.format(proxy), flush=True)
print('[launcher] exec   = {} {}'.format(sys.executable, MAIN), flush=True)

# main.py re-applies these same variables in-process before importing the
# project modules, because this host injects its own proxy at process creation.
sys.exit(subprocess.call([sys.executable, MAIN] + sys.argv[1:], cwd=HERE))
