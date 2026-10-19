"""Render a tweet as a screenshot-style card image.

The card mimics the X web layout: avatar, display name, verification badge,
handle, timestamp, body text, media, quoted tweet and engagement counts.  It
is rendered with Pillow instead of a headless browser so it stays fast (~1s)
and has no extra runtime to install on the runner.

Everything the reader needs ends up inside the picture; no link has to be
opened, which matters because x.com / pbs.twimg.com are unreachable from
mainland China without a proxy.
"""

import io
import os
import warnings
from datetime import timedelta, timezone

from PIL import Image, ImageDraw, ImageFont

from utils import (find_all, find_one, parse_create_time_from_tweet,
                   parse_media_from_tweet, parse_text_from_tweet)

# Tweet bodies that are just a URL trigger a spurious BeautifulSoup warning.
try:
    from bs4 import MarkupResemblesLocatorWarning
    warnings.filterwarnings('ignore', category=MarkupResemblesLocatorWarning)
except Exception:
    pass

WIDTH = 720
PAD = 24
BG = (255, 255, 255)
CARD = (247, 249, 249)
TEXT = (15, 20, 25)
MUTED = (83, 100, 113)
BORDER = (207, 217, 222)
ACCENT = (29, 155, 240)
AVATAR_BG = (207, 217, 222)

MAX_CANVAS_HEIGHT = 8000

_FONT_CANDIDATES = {
    False: [
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
        'C:/Windows/Fonts/msyh.ttc',
        'C:/Windows/Fonts/simhei.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ],
    True: [
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
        'C:/Windows/Fonts/msyhbd.ttc',
        'C:/Windows/Fonts/simhei.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    ],
}

_font_cache = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    for path in _FONT_CANDIDATES[bold]:
        if os.path.exists(path):
            try:
                _font_cache[key] = ImageFont.truetype(path, size, index=0)
                return _font_cache[key]
            except Exception:
                continue
    _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def _fetch(url: str) -> bytes | None:
    if not url:
        return None
    try:
        import requests
        response = requests.get(url, timeout=30)
        if response.status_code == 200 and response.content:
            return response.content
        return None
    except Exception:
        return None


def _load_image(url: str, fetcher) -> Image.Image | None:
    data = fetcher(url) if fetcher else _fetch(url)
    if not data:
        return None
    try:
        image = Image.open(io.BytesIO(data))
        return image.convert('RGB')
    except Exception:
        return None


def expand_urls(tweet: dict, text: str) -> str:
    """Replace t.co shorteners with the real target (t.co is blocked in CN)."""
    for items in find_all(tweet, 'urls'):
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            short = item.get('url')
            full = item.get('expanded_url')
            if short and full and short in text:
                text = text.replace(short, full)
    return text


def _fmt_count(value) -> str:
    try:
        number = int(str(value))
    except Exception:
        return str(value or '')
    if number >= 100_000_000:
        return '{:.1f}亿'.format(number / 100_000_000).replace('.0亿', '亿')
    if number >= 10_000:
        return '{:.1f}万'.format(number / 10_000).replace('.0万', '万')
    return str(number)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines = []
    for paragraph in text.split('\n'):
        if not paragraph:
            lines.append('')
            continue
        current = ''
        for char in paragraph:
            if draw.textlength(current + char, font=font) <= max_width:
                current += char
            else:
                lines.append(current)
                current = char
        lines.append(current)
    return lines


def _circle(image: Image.Image, size: int) -> Image.Image:
    image = image.resize((size, size), Image.LANCZOS)
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new('RGBA', (size, size))
    out.paste(image, (0, 0), mask)
    return out


def _rounded(image: Image.Image, radius: int) -> Image.Image:
    mask = Image.new('L', image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, image.size[0] - 1, image.size[1] - 1), radius=radius, fill=255)
    out = image.convert('RGBA')
    out.putalpha(mask)
    return out


def _avatar(tweet: dict, size: int, fetcher) -> Image.Image:
    author = find_one(tweet, 'user_results')
    url = find_one(author, 'profile_image_url_https') or ''
    url = url.replace('_normal', '_200x200')
    image = _load_image(url, fetcher) if url else None
    if image is None:
        image = Image.new('RGB', (size, size), AVATAR_BG)
    return _circle(image, size)


def _identity(tweet: dict) -> tuple[str, str, str]:
    author = find_one(tweet, 'user_results')
    core = find_one(author, 'core') or {}
    name = core.get('name') or ''
    handle = core.get('screen_name') or ''
    if not name or not handle:
        legacy = find_one(author, 'legacy') or {}
        name = name or legacy.get('name') or ''
        handle = handle or legacy.get('screen_name') or ''
    return name, handle, find_one(author, 'profile_image_url_https') or ''


def _verified(tweet: dict) -> bool:
    author = find_one(tweet, 'user_results')
    if find_one(author, 'is_blue_verified'):
        return True
    legacy = find_one(author, 'legacy') or {}
    return bool(legacy.get('verified'))


def _badge(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 18) -> None:
    draw.ellipse((x, y, x + size, y + size), fill=ACCENT)
    draw.line((x + size * 0.28, y + size * 0.52, x + size * 0.45, y + size * 0.68),
              fill=(255, 255, 255), width=max(2, size // 8))
    draw.line((x + size * 0.45, y + size * 0.68, x + size * 0.72, y + size * 0.34),
              fill=(255, 255, 255), width=max(2, size // 8))


def _media_grid(canvas: Image.Image, photos: list, x: int, y: int, width: int,
                fetcher) -> int:
    """Paste up to 4 photos. Returns the y coordinate below the grid."""
    if not photos:
        return y
    images = []
    for url in photos[:4]:
        image = _load_image(url, fetcher)
        if image is not None:
            images.append(image)
    if not images:
        return y

    if len(images) == 1:
        image = images[0]
        max_h = 420
        ratio = min(width / image.width, max_h / image.height, 1.0)
        size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
        image = _rounded(image.resize(size, Image.LANCZOS), 14)
        canvas.paste(image, (x, y), image)
        return y + size[1]

    columns = 2
    cell_w = (width - 4) // 2
    cell_h = int(cell_w * 0.75)
    for index, image in enumerate(images):
        row, column = divmod(index, columns)
        ratio = min(cell_w / image.width, cell_h / image.height)
        size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
        thumb = _rounded(image.resize(size, Image.LANCZOS), 12)
        cell_x = x + column * (cell_w + 4) + (cell_w - size[0]) // 2
        cell_y = y + row * (cell_h + 4) + (cell_h - size[1]) // 2
        canvas.paste(thumb, (cell_x, cell_y), thumb)
    rows = (len(images) + 1) // 2
    return y + rows * cell_h + (rows - 1) * 4


def _draw_icons(draw: ImageDraw.ImageDraw, x: int, y: int, counts: dict) -> None:
    color = MUTED
    small = _font(14)

    # reply
    draw.rounded_rectangle((x, y + 2, x + 15, y + 12), radius=3, outline=color, width=2)
    draw.line((x + 5, y + 12, x + 4, y + 16), fill=color, width=2)
    left = x + 22
    draw.text((left, y), counts['reply'], font=small, fill=color)
    x = left + int(draw.textlength(counts['reply'], font=small)) + 26

    # repost
    draw.line((x + 2, y + 6, x + 12, y + 6), fill=color, width=2)
    draw.polygon((x + 2, y + 2, x + 2, y + 10, x - 3, y + 6), fill=color)
    draw.line((x + 3, y + 13, x + 13, y + 13), fill=color, width=2)
    draw.polygon((x + 13, y + 9, x + 13, y + 17, x + 18, y + 13), fill=color)
    left = x + 24
    draw.text((left, y), counts['repost'], font=small, fill=color)
    x = left + int(draw.textlength(counts['repost'], font=small)) + 26

    # like
    draw.ellipse((x, y + 2, x + 7, y + 9), fill=color)
    draw.ellipse((x + 6, y + 2, x + 13, y + 9), fill=color)
    draw.polygon((x, y + 7, x + 13, y + 7, x + 6.5, y + 15), fill=color)
    left = x + 20
    draw.text((left, y), counts['like'], font=small, fill=color)
    x = left + int(draw.textlength(counts['like'], font=small)) + 26

    # views (bar chart)
    draw.rectangle((x + 1, y + 8, x + 4, y + 15), fill=color)
    draw.rectangle((x + 6, y + 4, x + 9, y + 15), fill=color)
    draw.rectangle((x + 11, y + 10, x + 14, y + 15), fill=color)
    left = x + 20
    draw.text((left, y), counts['views'], font=small, fill=color)


def _body_block(canvas: Image.Image, tweet: dict, x: int, y: int, width: int,
                scale: float, fetcher, with_footer: bool) -> int:
    """Draw header + text + media for one tweet. Returns the next y."""
    draw = ImageDraw.Draw(canvas)
    name, handle, _ = _identity(tweet)
    name_font = _font(int(19 * scale), bold=True)
    meta_font = _font(int(15 * scale))
    text_font = _font(int(19 * scale))

    stamp = ''
    try:
        created = parse_create_time_from_tweet(tweet)
        stamp = created.astimezone(timezone(timedelta(hours=8))).strftime('%m月%d日 %H:%M')
    except Exception:
        pass

    # name + badge
    draw.text((x, y), name, font=name_font, fill=TEXT)
    if _verified(tweet):
        _badge(draw, x + int(draw.textlength(name, font=name_font)) + 5,
               y + int(4 * scale), int(18 * scale))

    # handle · time
    meta = '@{}'.format(handle)
    if stamp:
        meta += ' · ' + stamp
    draw.text((x, y + int(26 * scale)), meta, font=meta_font, fill=MUTED)
    y += int(58 * scale)

    text = expand_urls(tweet, parse_text_from_tweet(tweet) or '')
    if text.strip():
        line_height = int(27 * scale)
        for line in _wrap(draw, text, text_font, width):
            draw.text((x, y), line, font=text_font, fill=TEXT)
            y += line_height
        y += int(6 * scale)

    photos, _videos = parse_media_from_tweet(tweet)
    if photos:
        y = _media_grid(canvas, photos, x, y, width, fetcher) + int(10 * scale)

    quoted = find_one(tweet, 'quoted_status_result')
    if isinstance(quoted, dict):
        quoted = quoted.get('result') or quoted
        if isinstance(quoted, dict):
            y = _quote_card(canvas, quoted, x, y, width, fetcher) + int(10 * scale)

    if with_footer:
        legacy = find_one(tweet, 'legacy') or {}
        views = find_one(tweet, 'views')
        view_count = views.get('count') if isinstance(views, dict) else ''
        counts = {
            'reply': _fmt_count(legacy.get('reply_count', 0)),
            'repost': _fmt_count(legacy.get('retweet_count', 0)),
            'like': _fmt_count(legacy.get('favorite_count', 0)),
            'views': _fmt_count(view_count or 0),
        }
        _draw_icons(ImageDraw.Draw(canvas), x, y + 6, counts)
        y += int(32 * scale)

    return y


def _quote_card(canvas: Image.Image, tweet: dict, x: int, y: int, width: int,
                fetcher) -> int:
    """Draw a quoted tweet inside a rounded card and return the next y."""
    height_limit = 4000
    overlay = Image.new('RGBA', (width, height_limit), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    inset = 14
    inner_w = width - inset * 2
    cursor = inset

    name, handle, _ = _identity(tweet)
    name_font = _font(16, bold=True)
    meta_font = _font(14)

    draw.text((inset, cursor), name, font=name_font, fill=TEXT)
    if _verified(tweet):
        _badge(draw, inset + int(draw.textlength(name, font=name_font)) + 4,
               cursor + 3, 15)
    draw.text((inset, cursor + 22), '@{}'.format(handle), font=meta_font, fill=MUTED)
    cursor += 44

    text_font = _font(16)
    text = expand_urls(tweet, parse_text_from_tweet(tweet) or '')
    if text.strip():
        for line in _wrap(draw, text, text_font, inner_w):
            draw.text((inset, cursor), line, font=text_font, fill=TEXT)
            cursor += 23
        cursor += 6

    photos, _videos = parse_media_from_tweet(tweet)
    if photos:
        cursor = _media_grid(overlay, photos, inset, cursor, inner_w, fetcher) + 8

    bottom = min(cursor + inset, height_limit)
    block = overlay.crop((0, 0, width, bottom))

    ImageDraw.Draw(canvas).rounded_rectangle(
        (x, y, x + width, y + bottom), radius=16, fill=CARD,
        outline=BORDER, width=1)
    canvas.paste(block, (x, y), block)
    return y + bottom


def render_card(tweet: dict, handle: str, fetcher=None) -> bytes:
    """Render one tweet as PNG bytes."""
    canvas = Image.new('RGB', (WIDTH, MAX_CANVAS_HEIGHT), BG)
    draw = ImageDraw.Draw(canvas)

    avatar_size = 48
    avatar = _avatar(tweet, avatar_size, fetcher)
    canvas.paste(avatar, (PAD, PAD), avatar)

    y = _body_block(canvas, tweet, PAD + avatar_size + 12, PAD,
                    WIDTH - PAD * 2 - avatar_size - 12, 1.0, fetcher,
                    with_footer=True)

    content_bottom = max(y, PAD + avatar_size) + PAD
    canvas = canvas.crop((0, 0, WIDTH, min(content_bottom, MAX_CANVAS_HEIGHT)))

    # hairline frame so the card reads as a screenshot on any background
    ImageDraw.Draw(canvas).rectangle(
        (0, 0, canvas.width - 1, canvas.height - 1), outline=(239, 243, 244), width=1)

    buffer = io.BytesIO()
    canvas.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()
