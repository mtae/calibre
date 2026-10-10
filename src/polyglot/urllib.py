#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPL v3 Copyright: 2018, Kovid Goyal <kovid at kovidgoyal.net>


from typing import TYPE_CHECKING

from urllib.parse import (parse_qs, quote, unquote as uq, quote_plus, urldefrag,  # noqa
        urlencode, urljoin, urlparse, urlunparse, urlsplit, urlunsplit)  # noqa

if TYPE_CHECKING:
    from urllib.error import HTTPError, URLError  # noqa
    from urllib.request import (build_opener, getproxies, install_opener,  # noqa
            HTTPBasicAuthHandler, HTTPCookieProcessor, HTTPDigestAuthHandler,  # noqa
            url2pathname, urlopen, Request)  # noqa

# urllib.request imports http.client, ssl, email and more, which is slow, so the
# names that come from urllib.request and urllib.error are imported when first
# used, see https://peps.python.org/pep-0562/
_lazy_names = {
    'build_opener': 'urllib.request',
    'getproxies': 'urllib.request',
    'install_opener': 'urllib.request',
    'HTTPBasicAuthHandler': 'urllib.request',
    'HTTPCookieProcessor': 'urllib.request',
    'HTTPDigestAuthHandler': 'urllib.request',
    'url2pathname': 'urllib.request',
    'urlopen': 'urllib.request',
    'Request': 'urllib.request',
    'HTTPError': 'urllib.error',
    'URLError': 'urllib.error',
}


def __getattr__(name):
    mod = _lazy_names.get(name)
    if mod is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    import importlib

    ans = getattr(importlib.import_module(mod), name)
    globals()[name] = ans
    return ans


def unquote(x, encoding='utf-8', errors='replace'):
    binary = isinstance(x, bytes)
    if binary:
        x = x.decode(encoding, errors)
    ans = uq(x, encoding, errors)
    if binary:
        ans = ans.encode(encoding, errors)
    return ans


def unquote_plus(x, encoding='utf-8', errors='replace'):
    q, repl = (b'+', b' ') if isinstance(x, bytes) else ('+', ' ')
    x = x.replace(q, repl)
    return unquote(x, encoding=encoding, errors=errors)
