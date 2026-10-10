#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

"""
Synthetic large library generator and benchmarks for the calibre database layer.

This is a measuring tool, not part of the automated test suite, apart from the
small smoke test at the bottom. It is meant to answer "what makes startup and
bulk operations slow" with numbers and to give later performance work a
yardstick. It does not change any calibre code, it only monkeypatches timing
wrappers around calibre functions inside the benchmark processes.

Generate a library (bulk SQLite writes, 50,000 books takes about a minute)::

    python src/calibre/db/tests/bench.py gen --books 50000 --dest /path/to/lib50k

Run benchmarks (every benchmark runs in a fresh process, timings are medians of
the repetitions, benchmarks that modify the library run on a hard linked copy)::

    python src/calibre/db/tests/bench.py run --lib /path/to/lib10k --lib /path/to/lib50k --reps 3
    python src/calibre/db/tests/bench.py run --lib /path/to/lib10k --bench open_cache,sort3
    python src/calibre/db/tests/bench.py list

Summarize ``python -X importtime`` for the modules the GUI imports at startup::

    python src/calibre/db/tests/bench.py importtime calibre.gui2.ui calibre.customize.ui

Time the main window startup of the GUI (offscreen) on a generated library::

    python src/calibre/db/tests/bench.py gui --lib /path/to/lib10k

Running the file directly works from a source checkout, provided calibre's C
extensions and resources have been built, no environment variables are needed.
Use the same python that is used to run the calibre tests, see the end of
``setup.py test`` for what it needs (``LD_LIBRARY_PATH`` of the PyQt6 Qt
libraries for the ``gui`` command and ``import calibre.gui2.ui``).
"""

import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from math import isnan

THIS_FILE = os.path.abspath(__file__)


def bootstrap_calibre():
    """Make calibre importable when this file is run as a script from a source
    checkout. Only needed in the processes that use calibre code, the parent
    process of the benchmark runner does not import calibre at all (calibre
    replaces the tempfile module, which starts a helper process)."""
    # The directory of this file contains modules (utils, locking, ...) that
    # must not shadow top level modules
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or '.') != os.path.dirname(THIS_FILE)]
    try:
        import calibre  # noqa: F401
    except ImportError:
        src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(THIS_FILE))))
        sys.path.insert(0, src)
        sys.resources_location = os.path.join(os.path.dirname(src), 'resources')
        sys.extensions_location = os.path.join(src, 'calibre', 'plugins')
        sys.executables_location = os.path.dirname(sys.executable)
        # calibre starts a helper process (calibre-parallel) to clean up its
        # temporary folder, which does not exist in a source checkout, so
        # provide the folder ourselves
        import atexit

        tdir = tempfile.mkdtemp(prefix='calibre-bench-')
        atexit.register(shutil.rmtree, tdir, True)
        import calibre.ptempfile

        calibre.ptempfile._base_dir = tdir


# Library generation {{{

CUSTOM_COLUMNS = (
    # label, name, datatype, is_multiple, display
    ('moods', 'Moods', 'text', True, {}),
    ('pages', 'Page count', 'int', False, {}),
    ('read', 'Read', 'bool', False, {}),
    ('summary', 'Summary', 'composite', False, {'composite_template': '{authors:shorten(25,0,0)} ({pubdate:format_date(yyyy)}) {#pages} p'}),
)

SAVED_SEARCHES = {
    'unrated': 'rating:false',
    'long books': '#pages:>400',
    'epub or mobi': 'formats:epub or formats:mobi',
    'tagged fantasy': 'tags:"=Genre.Fantasy" or tags:"~^Genre.Fantasy\\."',
}

VIRTUAL_LIBRARIES = {
    'Fantasy': 'tags:"~^Genre.Fantasy"',
    'Series books': 'series:true',
    'Recent': 'date:>2022-01-01',
    'Saved search': 'search:"long books"',
}

SYLLABLES = {
    'latin': 'ka lo mi ra ten sol ver an el dor ith mar que ben cas fi gri hol ju nys or pel quin sa tor ul vi wen xa yor zel de la ne po ro su ta'.split(),
    'latin_accents': 'é ça ñó öl ü ł ż ř ø å ã ê ï ß ğ ş ő ă'.split() + ['mé', 'sö', 'bü', 'wła', 'çel', 'ñor', 'hå', 'êt'],
    'cyrillic': 'ка ло ми ра тен сол вер ан ель дор ит мар ков ен ски'.split(),
    'greek': 'αλ ε ιο κα λο μι νο ξε πο ρο σι τα υ φι χα ψυ ω'.split(),
    'cjk': list('山川海空風花月雪星夢森光影夜朝春秋冬雲雨石木水火金土天地人心'),
    'arabic': 'كتا ب بحر نور ليل قمر شمس جبل وادي سما'.split(),
}
SCRIPT_WEIGHTS = (('latin', 70), ('latin_accents', 12), ('cyrillic', 6), ('cjk', 5), ('greek', 4), ('arabic', 3))
TOP_GENRES = ('Fiction', 'Fantasy', 'Science Fiction', 'History', 'Science', 'Biography', 'Mystery', 'Romance')
FORMAT_WEIGHTS = (('EPUB', 60), ('MOBI', 15), ('PDF', 15), ('AZW3', 6), ('TXT', 4))
LANGUAGES = (('eng', 70), ('deu', 6), ('fra', 5), ('spa', 4), ('rus', 4), ('jpn', 3), ('zho', 2), ('ita', 2), ('por', 1), ('pol', 1), ('ell', 1), ('ara', 1))
DATE_FMT = '%Y-%m-%d %H:%M:%S+00:00'
TINY_JPEG = (
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00'
    + b'\x01' * 64
    + b'\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4'
    b'\x00\x14\x00\x01' + b'\x00' * 15 + b'\n\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\xff\xd9'
)  # Not necessarily a decodable image, the library never decodes covers when opening


class Names:
    """Deterministic pseudo word generator with several scripts"""

    def __init__(self, rng):
        self.rng = rng
        self.scripts = [x[0] for x in SCRIPT_WEIGHTS]
        self.weights = [x[1] for x in SCRIPT_WEIGHTS]

    def word(self, script=None, syllables=None):
        rng = self.rng
        script = script or rng.choices(self.scripts, self.weights)[0]
        pool = SYLLABLES[script]
        base = SYLLABLES['latin'] if script == 'latin_accents' else pool
        n = syllables or rng.randint(2, 4)
        if script == 'cjk':
            return ''.join(rng.choices(pool, k=min(n, 3)))
        ans = ''.join(rng.choice(base) for _ in range(n))
        if script == 'latin_accents':
            ans += rng.choice(pool)
        return ans.capitalize()

    def person(self):
        script = self.rng.choices(self.scripts, self.weights)[0]
        return self.word(script, 2) + ' ' + self.word(script, 3)

    def title(self):
        rng = self.rng
        script = rng.choices(self.scripts, self.weights)[0]
        words = [self.word(script) for _ in range(rng.randint(1, 5))]
        if script.startswith('latin') and rng.random() < 0.2:
            words.insert(0, rng.choice(('The', 'A', 'An')))
        return ' '.join(words)


def weighted_choice_factory(rng, items):
    values = [x[0] for x in items]
    weights = [x[1] for x in items]
    return lambda: rng.choices(values, weights)[0]


def skewed_index(rng, n, power=2.0):
    """A random index in [0, n) with a bias towards low values, like real libraries
    where a few authors/tags account for many books."""
    return int(n * rng.random() ** power)


def generate_library(dest, num_books, seed=1, with_files=True, progress=print):
    """
    Create a realistic synthetic library with ``num_books`` books in the
    folder ``dest``, which must not contain a library already. Bulk inserts rows
    into the SQLite database and writes tiny format files and covers. Returns
    a dict with statistics about the library created.
    """
    from calibre.db.backend import DB
    from calibre.ebooks.metadata import author_to_author_sort

    t0 = time.monotonic()
    rng = random.Random(seed)
    names = Names(rng)
    dest = os.path.abspath(dest)
    if os.path.exists(os.path.join(dest, 'metadata.db')):
        raise ValueError(f'A library already exists at {dest}')
    os.makedirs(dest, exist_ok=True)
    backend = DB(dest)
    try:
        custom_nums = {}
        for label, name, datatype, is_multiple, display in CUSTOM_COLUMNS:
            custom_nums[label] = backend.create_custom_column(label, name, datatype, is_multiple, display=display)
        # Creating a custom column asks for the last modified dates of all books to be reset on next start
        backend.prefs.set('update_all_last_mod_dates_on_start', False)
        stats = _populate(backend, dest, num_books, rng, names, custom_nums, with_files, progress, author_to_author_sort)
        backend.prefs['saved_searches'] = dict(SAVED_SEARCHES)
        backend.prefs['virtual_libraries'] = dict(VIRTUAL_LIBRARIES)
        backend.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    finally:
        backend.close()
    stats['seconds'] = time.monotonic() - t0
    return stats


def _populate(backend, dest, num_books, rng, names, custom_nums, with_files, progress, author_to_author_sort):
    from calibre.utils.date import utcfromtimestamp  # noqa: F401  (ensures date machinery is initialized)

    execmany = backend.executemany
    num_authors = max(5, num_books // 5)
    num_series = max(5, num_books * 3 // 10 // 6)
    num_publishers = max(10, num_books // 50)
    num_moods = 200

    # Pools
    progress(f'Generating pools for {num_books} books')
    author_names = {}
    while len(author_names) < num_authors:
        n = names.person()
        author_names.setdefault(n, None)
    authors = list(author_names)
    author_sorts = [author_to_author_sort(a) for a in authors]
    tags = []
    seen_tags = set()
    mid = ('Epic', 'Urban', 'Classic', 'Modern', 'Dark', 'Cozy', 'Hard', 'Space')
    leaf = ('Early', 'Late', 'Anthology', 'Translated', 'Illustrated', 'Short', 'Long', 'Award')
    for g in TOP_GENRES:
        for m in mid:
            for l in leaf:
                tags.append(f'Genre.{g}.{m}.{l}')
    tags.append('Genre.Fantasy')
    while len(tags) < 2000:
        t = names.word().lower() if rng.random() < 0.7 else f'{names.word()} {names.word()}'
        if t not in seen_tags:
            seen_tags.add(t)
            tags.append(t)
    rng.shuffle(tags)
    tags = list(dict.fromkeys(tags))
    series_names = list(dict.fromkeys(names.title() + ' Saga' for _ in range(num_series)))
    publishers = list(dict.fromkeys(names.word() + ' ' + rng.choice(('Press', 'Books', 'Verlag', 'Publishing', 'Éditions')) for _ in range(num_publishers)))
    moods = list(dict.fromkeys(names.word().lower() for _ in range(num_moods)))
    num_moods = len(moods)
    lang_codes = [x[0] for x in LANGUAGES]
    pick_language = weighted_choice_factory(rng, LANGUAGES)
    pick_format = weighted_choice_factory(rng, FORMAT_WEIGHTS)
    pick_ratings = weighted_choice_factory(rng, ((0, 40), (2, 3), (4, 8), (6, 14), (8, 25), (10, 10)))

    with backend.conn:
        execmany('INSERT INTO authors(id,name,sort) VALUES (?,?,?)', ((i + 1, a, s) for i, (a, s) in enumerate(zip(authors, author_sorts))))
        execmany('INSERT INTO tags(id,name) VALUES (?,?)', ((i + 1, t) for i, t in enumerate(tags)))
        execmany('INSERT INTO series(id,name,sort) VALUES (?,?,?)', ((i + 1, s, s) for i, s in enumerate(series_names)))
        execmany('INSERT INTO publishers(id,name,sort) VALUES (?,?,?)', ((i + 1, s, s) for i, s in enumerate(publishers)))
        execmany('INSERT INTO languages(id,lang_code) VALUES (?,?)', ((i + 1, c) for i, c in enumerate(lang_codes)))
        execmany('INSERT INTO ratings(id,rating) VALUES (?,?)', ((i + 1, r) for i, r in enumerate((0, 2, 4, 6, 8, 10))))
        rating_ids = {r: i + 1 for i, r in enumerate((0, 2, 4, 6, 8, 10))}
        mood_table, mood_link = backend.custom_table_names(custom_nums['moods'])
        pages_table = backend.custom_table_names(custom_nums['pages'])[0]
        read_table = backend.custom_table_names(custom_nums['read'])[0]
        execmany(f'INSERT INTO {mood_table}(id,value) VALUES (?,?)', ((i + 1, m) for i, m in enumerate(moods)))

    # Per book rows
    progress('Generating book rows')
    rows_books, rows_authors, rows_tags, rows_series, rows_pub = [], [], [], [], []
    rows_rating, rows_lang, rows_ident, rows_comments, rows_data = [], [], [], [], []
    rows_mood, rows_pages, rows_read = [], [], []
    files = []  # (relative folder, [(file name, size)], has_cover)
    series_counts = {}
    ts_lo, ts_hi = time.mktime((2008, 1, 1, 0, 0, 0, 0, 0, 0)), time.mktime((2026, 6, 1, 0, 0, 0, 0, 0, 0))
    ntags = len(tags)
    stats = {'books': num_books, 'authors': len(authors), 'tags': ntags, 'series': len(series_names), 'publishers': len(publishers), 'formats': 0, 'covers': 0}
    construct_path_name, construct_file_name = backend.construct_path_name, backend.construct_file_name
    words = ('lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore magna aliqua').split()
    for book_id in range(1, num_books + 1):
        title = names.title()
        first = skewed_index(rng, len(authors), 1.6)
        author_ids = [first]
        r = rng.random()
        for _ in range(0 if r < 0.75 else 1 if r < 0.95 else 2):
            a = skewed_index(rng, len(authors), 1.6)
            if a not in author_ids:
                author_ids.append(a)
        author_sort = ' & '.join(author_sorts[a] for a in author_ids)
        ts = rng.uniform(ts_lo, ts_hi)
        pub = ts - rng.uniform(0, 86400 * 365 * 8)
        ts_s = time.strftime(DATE_FMT, time.gmtime(ts))
        has_cover = 1 if rng.random() < 0.6 else 0
        series_index = 1.0
        if rng.random() < 0.3:
            sid = skewed_index(rng, len(series_names), 1.3)
            series_counts[sid] = series_counts.get(sid, 0) + 1
            series_index = float(series_counts[sid])
            rows_series.append((book_id, sid + 1))
        path = construct_path_name(book_id, title, authors[first])
        rows_books.append((book_id, title, ts_s, time.strftime(DATE_FMT, time.gmtime(pub)), series_index, author_sort, path, has_cover, ts_s))
        rows_authors.extend((book_id, a + 1) for a in author_ids)
        k = rng.choices((0, 1, 2, 3, 4, 5, 6, 7, 8), (8, 14, 20, 20, 15, 10, 7, 4, 2))[0]
        if k:
            chosen = {skewed_index(rng, ntags, 2.2) for _ in range(k)}
            rows_tags.extend((book_id, t + 1) for t in chosen)
        if rng.random() < 0.8:
            rows_pub.append((book_id, skewed_index(rng, len(publishers), 1.5) + 1))
        rt = pick_ratings()
        if rt:
            rows_rating.append((book_id, rating_ids[rt]))
        lang = pick_language()
        rows_lang.append((book_id, lang_codes.index(lang) + 1, 0))
        if rng.random() < 0.05:
            other = rng.choice(lang_codes)
            if other != lang:
                rows_lang.append((book_id, lang_codes.index(other) + 1, 1))
        r = rng.random()
        if r < 0.7:
            rows_ident.append((book_id, 'isbn', str(9780000000000 + rng.randrange(10**9))))
        if r > 0.85:
            rows_ident.append((book_id, rng.choice(('goodreads', 'mobi-asin', 'amazon')), str(rng.randrange(10**8))))
        if rng.random() < 0.4:
            rows_comments.append((book_id, '<p>' + ' '.join(rng.choices(words, k=rng.randint(8, 40))) + '</p>'))
        k = rng.choices((0, 1, 2, 3), (50, 25, 15, 10))[0]
        if k:
            rows_mood.extend((book_id, m + 1) for m in {skewed_index(rng, num_moods, 1.5) for _ in range(k)})
        rows_pages.append((book_id, rng.randint(40, 1200)))
        if rng.random() < 0.7:
            rows_read.append((book_id, rng.random() < 0.5))
        fmts = [pick_format()]
        if rng.random() < 0.35:
            f2 = pick_format()
            if f2 not in fmts:
                fmts.append(f2)
        file_name = construct_file_name(book_id, title, authors[first], 4)
        sizes = []
        for fmt in fmts:
            size = 150 + (book_id * 7 + len(fmt)) % 300
            rows_data.append((book_id, fmt, size, file_name))
            sizes.append((f'{file_name}.{fmt.lower()}', size, fmt))
        stats['formats'] += len(fmts)
        stats['covers'] += has_cover
        files.append((path, sizes, has_cover))

    progress('Writing database rows')
    with backend.conn:
        execmany('INSERT INTO books(id,title,timestamp,pubdate,series_index,author_sort,path,has_cover,last_modified) VALUES (?,?,?,?,?,?,?,?,?)', rows_books)
        execmany('INSERT INTO books_authors_link(book,author) VALUES (?,?)', rows_authors)
        execmany('INSERT INTO books_tags_link(book,tag) VALUES (?,?)', rows_tags)
        execmany('INSERT INTO books_series_link(book,series) VALUES (?,?)', rows_series)
        execmany('INSERT INTO books_publishers_link(book,publisher) VALUES (?,?)', rows_pub)
        execmany('INSERT INTO books_ratings_link(book,rating) VALUES (?,?)', rows_rating)
        execmany('INSERT INTO books_languages_link(book,lang_code,item_order) VALUES (?,?,?)', rows_lang)
        execmany('INSERT INTO identifiers(book,type,val) VALUES (?,?,?)', rows_ident)
        execmany('INSERT INTO comments(book,text) VALUES (?,?)', rows_comments)
        execmany('INSERT INTO data(book,format,uncompressed_size,name) VALUES (?,?,?,?)', rows_data)
        execmany(f'INSERT INTO {mood_link}(book,value) VALUES (?,?)', rows_mood)
        execmany(f'INSERT INTO {pages_table}(book,value) VALUES (?,?)', rows_pages)
        execmany(f'INSERT INTO {read_table}(book,value) VALUES (?,?)', rows_read)
    # series_index of books in a series was set above, books not in a series keep 1.0

    if with_files:
        progress('Writing book files')
        cover_name = 'cover.jpg'
        for i, (path, sizes, has_cover) in enumerate(files):
            folder = os.path.join(dest, *path.split('/'))
            os.makedirs(folder, exist_ok=True)
            for name, size, fmt in sizes:
                with open(os.path.join(folder, name), 'wb') as f:
                    f.write((fmt.encode() + b' book data\n') * (size // 12 + 1))
            if has_cover:
                with open(os.path.join(folder, cover_name), 'wb') as f:
                    f.write(TINY_JPEG)
            if progress and i and i % 20000 == 0:
                progress(f'  {i} of {len(files)} book folders')
    return stats


# }}}

# Benchmarks {{{


def median(x):
    return statistics.median(x) if x else float('nan')


def tick():
    return time.perf_counter(), time.process_time()


class Timer:
    """Collects timings. Every timing is recorded as wall clock time and, under
    the same name with an ``@cpu`` suffix, as the CPU time of the process
    (all threads). The CPU time is much less sensitive to other processes
    running on the machine, the wall time includes waiting for the disk."""

    def __init__(self):
        self.times = {}

    def record(self, name, start, scale=1.0, accumulate=False):
        wall, cpu = time.perf_counter() - start[0], time.process_time() - start[1]
        if accumulate:
            self.times[name] = self.times.get(name, 0) + wall / scale
            self.times[name + '@cpu'] = self.times.get(name + '@cpu', 0) + cpu / scale
        else:
            self.times[name] = wall / scale
            self.times[name + '@cpu'] = cpu / scale

    def measure(self, name, func, *args, **kw):
        st = tick()
        try:
            return func(*args, **kw)
        finally:
            self.record(name, st)


def rss_mb():
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Linux reports KB


def disable_page_counting():
    """The background thread that counts the pages of books runs the counting
    in a helper process (calibre-parallel) that does not exist when running
    from a source checkout, so every added book would make it fail noisily and
    compete for the library lock. Its database updates (marking books as
    needing a scan) are still done."""
    from calibre.db.page_count import MaintainPageCounts

    MaintainPageCounts.queue_scan = lambda self, book_id=0: None


class CommitCounter:
    """Counts the SQLite transactions committed on the connection of a backend.
    Every commit is a durable write (fsync), the dominant cost of modifying
    operations on slow disks, so the count is the machine independent part of
    their cost."""

    def __init__(self, backend):
        self.count = 0
        backend.conn.set_commit_hook(self.hook)

    def hook(self):
        self.count += 1
        return False


def open_cache(path, timer=None, patch_tables=True):
    """Open a library with the new API, timing each phase and each table read.
    Returns the Cache."""
    from calibre.db import backend as backend_module
    from calibre.db.backend import DB
    from calibre.db.cache import Cache

    disable_page_counting()

    timer = timer or Timer()
    orig_read_table = DB._read_table
    orig_read_books_columns = backend_module.read_books_table_columns
    if patch_tables:

        def read_table(self, table, read, *args):
            st = tick()
            try:
                return orig_read_table(self, table, read, *args)
            finally:
                timer.record('table.' + table.name, st, accumulate=True)

        def read_books_columns(db, columns):
            st = tick()
            try:
                return orig_read_books_columns(db, columns)
            finally:
                timer.record('table.<books table columns>', st)

        DB._read_table = read_table
        backend_module.read_books_table_columns = read_books_columns
    orig_read_tables = DB.read_tables

    def read_tables(self):
        st = tick()
        try:
            return orig_read_tables(self)
        finally:
            timer.record('read_tables', st)

    DB.read_tables = read_tables
    try:
        st = tick()
        backend = timer.measure('DB()', DB, path)
        cache = Cache(backend)
        timer.measure('Cache.init', cache.init)
        timer.record('open_total', st)
    finally:
        DB._read_table = orig_read_table
        DB.read_tables = orig_read_tables
        backend_module.read_books_table_columns = orig_read_books_columns
    return cache


def open_legacy(path, timer=None):
    from calibre.db import legacy
    from calibre.db.view import View

    disable_page_counting()
    timer = timer or Timer()
    orig_init = View.__init__

    def view_init(self, cache):
        st = tick()
        try:
            return orig_init(self, cache)
        finally:
            timer.record('View()', st)

    View.__init__ = view_init
    orig_cache_init = legacy.Cache.init

    def cache_init(self):
        st = tick()
        try:
            return orig_cache_init(self)
        finally:
            timer.record('Cache.init', st)

    legacy.Cache.init = cache_init
    try:
        st = tick()
        db = legacy.LibraryDatabase(path)
        timer.record('open_total', st)
    finally:
        View.__init__ = orig_init
        legacy.Cache.init = orig_cache_init
    return db


def bench_open_cache(path):
    t = Timer()
    open_cache(path, t)
    t.times['rss_mb'] = rss_mb()
    return t.times


def bench_open_legacy(path):
    t = Timer()
    open_legacy(path, t)
    t.times['rss_mb'] = rss_mb()
    return t.times


def bench_sort3(path):
    t = Timer()
    cache = open_cache(path, patch_tables=False)
    spec = [('title', True), ('authors', True), ('series', True)]
    t.measure('first', cache.multisort, spec)
    t.measure('second', cache.multisort, spec)
    t.measure('desc_first', cache.multisort, [('title', False)])
    return t.times


def bench_sort_composite(path):
    t = Timer()
    cache = open_cache(path, patch_tables=False)
    t.measure('first', cache.multisort, [('#summary', True)])
    t.measure('second', cache.multisort, [('#summary', True)])
    return t.times


def bench_categories(path):
    t = Timer()
    cache = open_cache(path, patch_tables=False)
    t.measure('all_novl_first', cache.get_categories)
    t.measure('all_novl_second', cache.get_categories)
    ids = cache.books_in_virtual_library('Fantasy')
    t.times['vl_books'] = len(ids)
    t.measure('all_with_vl', cache.get_categories, book_ids=ids)
    ids = cache.books_in_virtual_library('Series books')
    t.times['vl2_books'] = len(ids)
    t.measure('all_with_vl2', cache.get_categories, book_ids=ids)
    return t.times


SEARCHES = (
    ('title_contains', 'title:the'),
    ('all_fields', 'ka'),
    ('tags_equals', 'tags:"=Genre.Fantasy"'),
    ('tags_regex', 'tags:"~^Genre.Fiction"'),
    ('authors', 'authors:ra'),
    ('int_range', '#pages:>400'),
    ('rating_false', 'rating:false'),
    ('format_and_not_series', 'formats:epub and not series:true'),
    ('composite', '#summary:2019'),
    ('date', 'date:>2022-01-01'),
)


def bench_search(path):
    t = Timer()
    cache = open_cache(path, patch_tables=False)
    for name, query in SEARCHES:
        t.measure(name, cache.search, query)
    t.measure('repeat_all_fields', cache.search, 'ka')
    return t.times


def bench_virtual_library(path):
    t = Timer()
    cache = open_cache(path, patch_tables=False)
    vls = cache._pref('virtual_libraries', {})
    for name in vls:
        t.measure('apply.' + name, cache.books_in_virtual_library, name)
    t.measure('virtual_libraries_for_books_all', cache.virtual_libraries_for_books, cache.all_book_ids())
    return t.times


def make_epub_like_files(folder, count):
    os.makedirs(folder, exist_ok=True)
    ans = []
    for i in range(count):
        p = os.path.join(folder, f'new book {i}.epub')
        with open(p, 'wb') as f:
            f.write(b'PK\x03\x04 not a real epub ' + str(i).encode() * 20)
        ans.append(p)
    return ans


def bench_add_new_api(path):
    from calibre.ebooks.metadata.book.base import Metadata

    t = Timer()
    cache = open_cache(path, patch_tables=False)
    tmp = tempfile.mkdtemp(prefix='bench_add_')
    try:
        files = make_epub_like_files(tmp, 100)
        books = []
        for i, p in enumerate(files):
            mi = Metadata(f'Added Book {i}', [f'Added Author {i % 7}'])
            mi.tags = ['added', f'added{i % 5}']
            books.append((mi, {'EPUB': p}))
        cc = CommitCounter(cache.backend)
        t.measure('add_100', cache.add_books, books)
        t.times['add_100_commits'] = cc.count
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return t.times


def bench_add_legacy_api(path):
    from calibre.ebooks.metadata.book.base import Metadata

    t = Timer()
    db = open_legacy(path)
    tmp = tempfile.mkdtemp(prefix='bench_add_')
    try:
        files = make_epub_like_files(tmp, 100)
        mis = []
        for i in range(len(files)):
            mi = Metadata(f'Added Book {i}', [f'Added Author {i % 7}'])
            mi.tags = ['added', f'added{i % 5}']
            mis.append(mi)
        cc = CommitCounter(db.backend)
        t.measure('add_books_100', db.add_books, files, ['epub'] * len(files), mis)
        t.times['add_books_100_commits'] = cc.count
        cc.count = 0
        t.measure('import_book_x20', lambda: [db.import_book(mi, [files[i]]) for i, mi in enumerate(mis[:20])])
        t.times['import_book_x20_commits'] = cc.count
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return t.times


def bench_delete(path):
    """Delete 500 books like the GUI does (MultiDeleter): chunks of 100 books
    through Cache.remove_books(permanent=False), the view notified for every
    chunk, then a commit and clean at the end."""
    t = Timer()
    db = open_legacy(path)
    ids = sorted(db.new_api.all_book_ids())[::7][:500]
    chunk_size = 100
    cc = CommitCounter(db.backend)
    st = tick()
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        db.new_api.remove_books(chunk, permanent=False)
        db.data.books_deleted(tuple(chunk))
    t.record('delete_500_chunks', st)
    t.times['delete_500_commits'] = cc.count
    t.measure('commit_clean', lambda: (db.commit(), db.clean()))
    return t.times


def bench_set_tags(path):
    t = Timer()
    cache = open_cache(path, patch_tables=False)
    ids = sorted(cache.all_book_ids())[::3][:1000]
    cc = CommitCounter(cache.backend)
    t.measure('set_tags_1000_one_call', cache.set_field, 'tags', {b: ('bench tag', f'bench tag {b % 10}') for b in ids})
    t.times['set_tags_1000_one_call_commits'] = cc.count
    ids2 = sorted(cache.all_book_ids())[1::3][:100]
    cc.count = 0
    st = tick()
    for b in ids2:
        cache.set_field('tags', {b: ('bench two',)})
    t.record('set_tags_per_call_100_calls', st, scale=len(ids2))
    t.times['set_tags_per_call_commits'] = cc.count / len(ids2)
    return t.times


def bench_machine(path):
    """Costs of the machine that explain the cost of other benchmarks: durable
    SQLite commits (limited by the speed of fsync) and creating small files.
    Done in the folder of the library so that it measures its filesystem."""
    import sqlite3

    t = Timer()
    d = tempfile.mkdtemp(prefix='machine_', dir=path)
    try:
        conn = sqlite3.connect(os.path.join(d, 'x.db'), isolation_level=None)
        conn.execute('CREATE TABLE t(a)')
        st = tick()
        for i in range(100):
            conn.execute('INSERT INTO t VALUES (1)')
        t.record('sqlite_commit_each', st, scale=100)
        conn.close()
        st = tick()
        for i in range(500):
            with open(os.path.join(d, f'f{i}'), 'wb') as f:
                f.write(b'x' * 200)
        t.record('create_small_file', st, scale=500)
        st = tick()
        for i in range(500):
            os.rename(os.path.join(d, f'f{i}'), os.path.join(d, f'g{i}'))
        t.record('rename_file', st, scale=500)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return t.times


# name: (function, what the benchmark modifies: False, 'db' (only metadata.db,
# no book files are needed) or 'files' (book files are moved/removed), description)
BENCHMARKS = {
    'machine': (bench_machine, False, 'cost of a durable SQLite commit and of creating small files in the library folder'),
    'open_cache': (bench_open_cache, False, 'DB() + Cache.init() with per table read times'),
    'open_legacy': (bench_open_legacy, False, 'LibraryDatabase() (backend, Cache.init, View)'),
    'sort3': (bench_sort3, False, 'multisort by title, authors, series'),
    'sort_composite': (bench_sort_composite, False, 'multisort by a composite column'),
    'categories': (bench_categories, False, 'get_categories() without and with a virtual library'),
    'search': (bench_search, False, 'a set of searches over all books'),
    'virtual_library': (bench_virtual_library, False, 'apply each virtual library, vls_for_books'),
    'add_new_api': (bench_add_new_api, 'db', 'Cache.add_books() of 100 books'),
    'add_legacy_api': (bench_add_legacy_api, 'db', 'LibraryDatabase.add_books() of 100 books, import_book() of 20'),
    'delete': (bench_delete, 'files', 'delete 500 books in chunks of 100 like MultiDeleter'),
    'set_tags': (bench_set_tags, 'db', 'set_field tags on 1000 books'),
}


def copy_library(src, dest, mode):
    """Copy a library so that the benchmarks can modify it. With mode 'db' only
    the database and notes are copied, with 'files' the book files are hard
    linked as well (the benchmarks only move or remove them, they never modify
    them), which needs ``dest`` to be on the same filesystem as ``src``."""
    if mode == 'files':
        subprocess.check_call(['cp', '-al', src, dest])
    else:
        os.makedirs(dest)
    # Databases are modified in place, so they must not be hard links
    for name in os.listdir(src):
        if name.startswith('metadata.db') or name == '.calnotes':
            s, d = os.path.join(src, name), os.path.join(dest, name)
            if os.path.isdir(s):
                shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(s, d)
            else:
                if os.path.exists(d):
                    os.remove(d)
                shutil.copy2(s, d)


def worker_env(workdir):
    env = os.environ.copy()
    env['CALIBRE_CONFIG_DIRECTORY'] = os.path.join(workdir, 'config')
    env['CALIBRE_CACHE_DIRECTORY'] = os.path.join(workdir, 'cache')
    env['CALIBRE_TEMP_DIR'] = os.path.join(workdir, 'tmp')
    env['QT_QPA_PLATFORM'] = env.get('QT_QPA_PLATFORM', 'offscreen')
    env.pop('CALIBRE_OVERRIDE_DATABASE_PATH', None)
    for x in ('config', 'cache', 'tmp'):
        os.makedirs(os.path.join(workdir, x), exist_ok=True)
    return env


def run_worker(name, libpath, workdir, timeout=3600, profile=None):
    env = worker_env(workdir)
    cmd = [sys.executable, THIS_FILE, '_worker', name, libpath]
    if profile:
        cmd += ['--profile', profile]
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f'Benchmark {name} failed:\n{p.stdout[-2000:]}\n{p.stderr[-4000:]}')
    for line in reversed(p.stdout.splitlines()):
        if line.startswith('BENCH_RESULT '):
            return json.loads(line[len('BENCH_RESULT ') :])
    raise RuntimeError(f'Benchmark {name} printed no result:\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}')


def run_benchmarks(libs, bench_names, reps, workroot, out=print):
    """Returns {lib: {bench: {key: [values over reps]}}}. The libraries are
    interleaved (for every repetition and benchmark each library in turn) so
    that changes of the load of the machine affect all of them similarly."""
    libs = [os.path.abspath(lib) for lib in libs]
    results = {lib: {} for lib in libs}
    for rep in range(reps):
        for name in bench_names:
            for lib in libs:
                func, modifies, desc = BENCHMARKS[name]
                workdir = os.path.join(workroot, 'work')
                path = lib
                if modifies:
                    path = os.path.join(workroot, 'copy')
                    shutil.rmtree(path, ignore_errors=True)
                    copy_library(lib, path, modifies)
                st = time.monotonic()
                res = run_worker(name, path, workdir)
                if modifies:
                    shutil.rmtree(path, ignore_errors=True)
                out(f'  rep {rep + 1}/{reps} {name} {os.path.basename(lib)}: {time.monotonic() - st:.1f}s wall (including setup)', flush=True)
                for k, v in res.items():
                    results[lib].setdefault(name, {}).setdefault(k, []).append(v)
    return results


def format_results(results, show_tables=True):
    """A table of medians. Every timing is shown as wall time with the CPU time
    of the process in parentheses, the ratio is that of the CPU time."""
    libs = list(results)
    sizes = {lib: _lib_size(lib) for lib in libs}
    header = ['benchmark.key'] + [f'{sizes[lib]} books' for lib in libs]
    if len(libs) >= 2:
        header.append(f'ratio {sizes[libs[-1]]}/{sizes[libs[0]]}')
    rows = []
    keys = {}
    for lib in libs:
        for bench, d in results[lib].items():
            for k in d:
                keys.setdefault(bench, {})[k] = True
    for bench in BENCHMARKS:
        if bench not in keys:
            continue
        for k in keys[bench]:
            if k.endswith('@cpu') or (k.startswith('table.') and not show_tables):
                continue
            vals = [median(results[lib].get(bench, {}).get(k, [])) for lib in libs]
            cpus = [median(results[lib].get(bench, {}).get(k + '@cpu', [])) for lib in libs]
            row = [f'{bench}.{k}'] + [_fmt(k, v, c) for v, c in zip(vals, cpus)]
            if len(libs) >= 2:
                a, b = (cpus[0], cpus[-1]) if not isnan(cpus[0]) else (vals[0], vals[-1])
                row.append(f'{b / a:.1f}x' if a and not isnan(a) and not isnan(b) else '')
            rows.append(row)
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(len(header))]
    lines = ['  '.join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths))) for r in [header] + rows]
    return '\n'.join(lines)


def _fmt(key, v, cpu=float('nan')):
    if isnan(v):
        return '-'
    if key == 'rss_mb':
        return f'{v:.0f} MB'
    if key.endswith(('_books', 'commits')):
        return f'{v:.1f}' if v != int(v) else f'{v:.0f}'
    if not isnan(cpu):
        return f'{v:.3f}s ({cpu:.3f})'
    return f'{v:.3f}s'


def _lib_size(lib):
    import sqlite3

    conn = sqlite3.connect(f'file:{lib}/metadata.db?mode=ro', uri=True)
    try:
        return conn.execute('SELECT COUNT(*) FROM books').fetchone()[0]
    finally:
        conn.close()


# }}}

# importtime and GUI startup {{{


def parse_importtime(stderr):
    """Parse the output of python -X importtime: returns (total_us, [(self_us, cumulative_us, module)])"""
    mods = []
    for line in stderr.splitlines():
        if not line.startswith('import time:'):
            continue
        parts = line[len('import time:') :].split('|')
        if len(parts) != 3:
            continue
        try:
            self_us, cum = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        name = parts[2].strip()
        indent = len(parts[2]) - len(parts[2].lstrip(' ')) - 1
        mods.append((self_us, cum, name, indent))
    return mods


IMPORT_SNIPPET = '''
import sys, os, time
src, module = sys.argv[1], sys.argv[2]
sys.path.insert(0, src)
sys.resources_location = os.path.join(os.path.dirname(src), 'resources')
sys.extensions_location = os.path.join(src, 'calibre', 'plugins')
sys.executables_location = os.path.dirname(sys.executable)
import tempfile, atexit, shutil
tdir = tempfile.mkdtemp(prefix='calibre-bench-')
atexit.register(shutil.rmtree, tdir, True)
st = time.perf_counter()
import calibre.ptempfile
calibre.ptempfile._base_dir = tdir
import importlib
importlib.import_module(module)
print('IMPORT_WALL', time.perf_counter() - st)
'''


def import_package(name):
    parts = name.split('.')
    return '.'.join(parts[:2]) if parts[0] == 'calibre' and len(parts) > 1 else parts[0]


def summarize_importtime(module, workdir, reps=5, top=12):
    """Runs fresh interpreters with -X importtime that import nothing but the
    module (this file is not imported, so its own imports are not counted).
    Returns medians over the repetitions of the import wall time and of the
    time spent in each package (the self time of its modules, in ms)."""
    env = worker_env(workdir)
    src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(THIS_FILE))))
    walls, groups, selfs, cums, counts = [], [], [], [], []
    for _ in range(reps):
        p = subprocess.run([sys.executable, '-X', 'importtime', '-c', IMPORT_SNIPPET, src, module], env=env, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f'Importing {module} failed:\n{p.stderr[-3000:]}')
        walls.append(float([l for l in p.stdout.splitlines() if l.startswith('IMPORT_WALL')][-1].split()[1]))
        mods = parse_importtime(p.stderr)
        counts.append(len(mods))
        g, sf, cm = {}, {}, {}
        for self_us, cum, name, indent in mods:
            g[import_package(name)] = g.get(import_package(name), 0) + self_us / 1e3
            sf[name] = self_us / 1e3
            if indent <= 2:
                cm[name] = cum / 1e3
        groups.append(g)
        selfs.append(sf)
        cums.append(cm)

    def med(listofdicts):
        keys = set().union(*listofdicts)
        return sorted(((k, median([d.get(k, 0) for d in listofdicts])) for k in keys), key=lambda x: -x[1])[:top]

    return {
        'module': module,
        'wall_s': median(walls),
        'reps': reps,
        'modules_imported': median(counts),
        'top_self': med(selfs),
        'top_cumulative': med(cums),
        'groups': med(groups),
    }


def format_importtime(s):
    out = [f"{s['module']}: {s['modules_imported']:.0f} modules, import wall {s['wall_s']:.2f}s (median of {s['reps']}, interpreter startup not included)"]
    out.append('  by package (self ms): ' + ', '.join(f'{g} {v:.0f}' for g, v in s['groups']))
    out.append('  top modules by self ms: ' + ', '.join(f'{n} {v:.0f}' for n, v in s['top_self']))
    out.append('  top by cumulative ms (depth <= 2): ' + ', '.join(f'{n} {v:.0f}' for n, v in s['top_cumulative']))
    return '\n'.join(out)


def gui_startup(libpath, workdir, timeout=900, profile=None):
    """Start the GUI offscreen on the library and capture the timed_print
    checkpoints that calibre prints in debug mode. The process quits as soon
    as calibre reports that it has started up."""
    env = worker_env(workdir)
    cmd = [sys.executable, THIS_FILE, '_gui', libpath]
    if profile:
        cmd += ['--profile', profile]
    st = time.monotonic()
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    wall = time.monotonic() - st
    return p.returncode, wall, p.stdout, p.stderr


def gui_worker(libpath, profile=None):
    """Runs in the process that hosts the GUI. Marks the welcome wizard as
    done, forces calibre's debug mode so that timed_print prints its
    checkpoints, and quits the event loop once startup is complete."""
    import calibre
    from calibre import constants

    constants.DEBUG = True
    from calibre.utils.config import dynamic

    dynamic.set('welcome_wizard_was_run', True)
    orig_call = calibre.TimedPrint.__call__

    def timed_call(self, *a, **kw):
        orig_call(self, *a, **kw)
        if a and isinstance(a[0], str) and a[0].startswith('Started up in'):
            from qt.core import QApplication, QTimer

            QTimer.singleShot(1000, QApplication.instance().quit)

    calibre.TimedPrint.__call__ = timed_call
    from qt.core import QStyleHints

    if not hasattr(QStyleHints, 'colorSchemeChanged'):
        # Qt older than 6.5, the calibre theme code cannot work, so use a
        # simplified palette set up. Startup times are then only roughly right.
        from calibre.gui2 import palette

        def setup_styles(self):
            self.using_calibre_style = False
            self.on_palette_change = lambda *a, **kw: None

        palette.PaletteManager.setup_styles = setup_styles
    from calibre.gui2.main import main as gui_main

    args = ['calibre', '--with-library', libpath]
    if profile:
        import cProfile

        pr = cProfile.Profile()
        pr.enable()
        try:
            gui_main(args)
        finally:
            pr.disable()
            pr.dump_stats(profile)
    else:
        gui_main(args)
    sys.stdout.flush()
    os._exit(0)


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('gen', help='generate a synthetic library')
    g.add_argument('--books', type=int, required=True)
    g.add_argument('--dest', required=True)
    g.add_argument('--seed', type=int, default=1)
    g.add_argument('--no-files', action='store_true', help='only write the database, not the book files')
    r = sub.add_parser('run', help='run benchmarks')
    r.add_argument('--lib', action='append', required=True, help='library to use, can be given several times (ascending size)')
    r.add_argument('--bench', default='all', help='comma separated benchmark names, see "list"')
    r.add_argument('--reps', type=int, default=3)
    r.add_argument('--workdir', default=None)
    r.add_argument('--json', default=None, help='save the raw results to this file')
    r.add_argument('--no-tables', action='store_true', help='do not show the per table read times')
    sub.add_parser('list', help='list the benchmarks')
    i = sub.add_parser('importtime', help='summarize python -X importtime')
    i.add_argument('modules', nargs='+')
    i.add_argument('--reps', type=int, default=5)
    gu = sub.add_parser('gui', help='time the GUI main window startup (offscreen)')
    gu.add_argument('--lib', required=True)
    gu.add_argument('--profile', default=None, help='save cProfile stats of the whole startup (main thread) to this file')
    w = sub.add_parser('_worker')
    w.add_argument('name')
    w.add_argument('lib')
    w.add_argument('--profile', default=None)
    pr = sub.add_parser('profile', help='run one benchmark under cProfile and print the most expensive functions')
    pr.add_argument('--lib', required=True)
    pr.add_argument('--bench', required=True)
    pr.add_argument('--top', type=int, default=30)
    pr.add_argument('--sort', default='cumulative', choices=('cumulative', 'tottime'))
    gi = sub.add_parser('_gui')
    gi.add_argument('lib')
    gi.add_argument('--profile', default=None)
    args = ap.parse_args(argv)
    if args.cmd in ('gen', '_worker', '_gui'):
        bootstrap_calibre()

    if args.cmd == 'gen':
        stats = generate_library(args.dest, args.books, seed=args.seed, with_files=not args.no_files)
        print(json.dumps(stats))
    elif args.cmd == 'list':
        for name, (func, modifies, desc) in BENCHMARKS.items():
            print(f'{name:18} {"(runs on a copy) " if modifies else ""}{desc}')
    elif args.cmd == 'run':
        names = list(BENCHMARKS) if args.bench == 'all' else args.bench.split(',')
        for n in names:
            if n not in BENCHMARKS:
                raise SystemExit(f'Unknown benchmark {n}, see the list command')
        workroot = args.workdir or tempfile.mkdtemp(prefix='calibre_bench_', dir=os.path.dirname(os.path.abspath(args.lib[0])))
        os.makedirs(workroot, exist_ok=True)
        results = run_benchmarks(args.lib, names, args.reps, workroot)
        if args.json:
            with open(args.json, 'w') as f:
                json.dump(results, f, indent=1)
        print(format_results(results, show_tables=not args.no_tables))
    elif args.cmd == 'profile':
        import pstats

        workroot = tempfile.mkdtemp(prefix='calibre_bench_', dir=os.path.dirname(os.path.abspath(args.lib)))
        try:
            func, modifies, desc = BENCHMARKS[args.bench]
            path = args.lib
            if modifies:
                path = os.path.join(workroot, 'copy')
                copy_library(args.lib, path, modifies)
            stats_file = os.path.join(workroot, 'stats')
            res = run_worker(args.bench, path, os.path.join(workroot, 'work'), profile=stats_file)
            print({k: round(v, 3) for k, v in res.items() if not k.startswith('table.')})
            pstats.Stats(stats_file).sort_stats(args.sort).print_stats(args.top)
        finally:
            shutil.rmtree(workroot, ignore_errors=True)
    elif args.cmd == 'importtime':
        workdir = tempfile.mkdtemp(prefix='calibre_bench_')
        for m in args.modules:
            print(format_importtime(summarize_importtime(m, workdir, args.reps)))
    elif args.cmd == 'gui':
        workdir = tempfile.mkdtemp(prefix='calibre_bench_')
        rc, wall, out, err = gui_startup(args.lib, workdir, profile=args.profile)
        print(out)
        print(err[-4000:])
        print(f'exit code {rc}, wall {wall:.1f}s')
    elif args.cmd == '_worker':
        func = BENCHMARKS[args.name][0]
        if args.profile:
            import cProfile

            pr = cProfile.Profile()
            res = pr.runcall(func, args.lib)
            pr.dump_stats(args.profile)
        else:
            res = func(args.lib)
        print('BENCH_RESULT ' + json.dumps(res))
        sys.stdout.flush()
        os._exit(0)  # do not wait for threads of the library
    elif args.cmd == '_gui':
        gui_worker(args.lib, args.profile)


class LibraryBenchSmokeTest(unittest.TestCase):
    """Checks that the generator makes a valid library and that the opening
    and basic operations are not unreasonably slow on it. The limits are very
    generous (many times the measured times), this is not a performance test,
    it only catches the library layer becoming dramatically slower."""

    num_books = 1000

    def test_generated_library(self):
        from calibre.db.page_count import MaintainPageCounts
        from calibre.library.check_library import CheckLibrary

        # open_legacy() disables page counting by replacing a method of the
        # class, which would otherwise make every later test in this process
        # (for e.g. WritingTest.test_composite_cache) wait forever for the
        # page counting thread to process a queued book
        self.addCleanup(setattr, MaintainPageCounts, 'queue_scan', MaintainPageCounts.queue_scan)

        tdir = tempfile.mkdtemp()
        try:
            lib = os.path.join(tdir, 'lib')
            stats = generate_library(lib, self.num_books, progress=lambda *a: None)
            self.assertEqual(stats['books'], self.num_books)
            db = open_legacy(lib)
            try:
                cache = db.new_api
                self.assertEqual(len(cache.all_book_ids()), self.num_books)
                checker = CheckLibrary(lib, db)
                checker.scan_library([], [])
                for attr in (
                    'invalid_titles',
                    'extra_titles',
                    'invalid_authors',
                    'extra_authors',
                    'missing_formats',
                    'extra_formats',
                    'extra_files',
                    'missing_covers',
                    'extra_covers',
                    'malformed_formats',
                    'malformed_paths',
                    'failed_folders',
                ):
                    self.assertFalse(getattr(checker, attr, None), attr)
                st = time.perf_counter()
                cache.multisort([('title', True), ('authors', True), ('series', True)])
                cats = cache.get_categories()
                self.assertTrue(cats['tags'])
                self.assertTrue(cache.search('formats:epub'))
                self.assertTrue(cache.books_in_virtual_library('Fantasy'))
                self.assertTrue(cache.books_in_virtual_library('Series books'))
                self.assertEqual(set(cache.saved_search_names()), set(SAVED_SEARCHES))
                self.assertLess(time.perf_counter() - st, 60, 'sorting, categories and searching a generated library is very slow')
            finally:
                db.close()
        finally:
            shutil.rmtree(tdir, ignore_errors=True)


if __name__ == '__main__':
    main()
