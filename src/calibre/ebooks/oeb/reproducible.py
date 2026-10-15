#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, calibre contributors

"""
Make the values calibre invents while converting a book (build timestamps,
random ids) injectable, so that tests and reproducible builds can get
byte-identical output. By default nothing changes: real timestamps and random
ids are used. If the SOURCE_DATE_EPOCH environment variable
(https://reproducible-builds.org/specs/source-date-epoch/) is set, it is used as
the current time and the random values are replaced by values derived from it
and from the order in which they are requested.

Not covered, because they are not inventions of the conversion code: the
modification times of the entries in a generated EPUB zip file (taken from the
temporary files it is built from), and the default generated cover (random
colors and layout), identifiers the input book does not supply. Supply a cover,
an OPF with identifiers/timestamp via --read-metadata-from-opf and mask the zip
times when comparing.
"""

import hashlib
import itertools
import os
import time
import uuid
import zlib

_counter = itertools.count()


def source_date_epoch():
    try:
        return int(os.environ['SOURCE_DATE_EPOCH'])
    except KeyError, ValueError:
        return None


def reset():
    "Restart the sequence of derived values, for several conversions in one process"
    global _counter
    _counter = itertools.count()


def epoch_seconds():
    "Current time, as integer seconds since the epoch"
    ans = source_date_epoch()
    return int(time.time()) if ans is None else ans


def random_bytes(n):
    "n random bytes (used to obfuscate fonts)"
    sde = source_date_epoch()
    if sde is None:
        return os.urandom(n)
    seq, ans = next(_counter), b''
    while len(ans) < n:
        ans += hashlib.sha256(f'bytes:{sde}:{seq}:{len(ans)}'.encode()).digest()
    return ans[:n]


def datetime_now():
    "Like calibre.utils.date.now()"
    from calibre.utils.date import now, utcfromtimestamp

    sde = source_date_epoch()
    return now() if sde is None else utcfromtimestamp(sde)


def random_uint32():
    "A random unsigned 32 bit number (used for the unique ids in MOBI headers)"
    sde = source_date_epoch()
    if sde is None:
        import random

        return random.randint(0, 0xFFFFFFFF)
    return zlib.crc32(f'uint32:{sde}:{next(_counter)}'.encode())


def short_uuid4():
    "Like calibre.utils.short_uuid.uuid4()"
    from calibre.utils import short_uuid

    sde = source_date_epoch()
    if sde is None:
        return short_uuid.uuid4()
    return short_uuid.uuid5(uuid.NAMESPACE_OID, f'short_uuid4:{sde}:{next(_counter)}')


def uuid4():
    "A random UUID4 or, if SOURCE_DATE_EPOCH is set, a UUID derived from it"
    sde = source_date_epoch()
    if sde is None:
        return uuid.uuid4()
    return uuid.uuid5(uuid.NAMESPACE_OID, f'uuid4:{sde}:{next(_counter)}')
