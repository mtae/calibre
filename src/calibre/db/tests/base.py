#!/usr/bin/env python
# License: GPLv3 Copyright: 2011, Kovid Goyal <kovid@kovidgoyal.net>

import gc
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from functools import partial
from io import BytesIO

from calibre.constants import iswindows
from calibre.utils.resources import get_image_path as I

rmtree = partial(shutil.rmtree, ignore_errors=True)

IMG = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xe1\x00\x16Exif\x00\x00II*\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xdb\x00C\x00\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\xff\xdb\x00C\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01"\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x15\x00\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\n\xff\xc4\x00\x14\x10\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xc4\x00\x14\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xc4\x00\x14\x11\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xbf\x80\x01\xff\xd9'  # noqa: E501


class BaseTest(unittest.TestCase):
    longMessage = True
    maxDiff = None

    def setUp(self):
        from calibre.utils.recycle_bin import nuke_recycle

        nuke_recycle()
        self.paths_to_remove = []
        self.objects_to_close = []
        self.library_path = self.mkdtemp()
        self.create_db(self.library_path)

    def tearDown(self):
        from calibre.utils.recycle_bin import restore_recyle

        restore_recyle()
        for x in self.objects_to_close:
            x.close()
        self.objects_to_close = []
        gc.collect(), gc.collect()
        for x in self.paths_to_remove:
            try:
                shutil.rmtree(x)
            except OSError:
                if iswindows:
                    import atexit

                    atexit.register(shutil.rmtree, x)
                else:
                    import time

                    time.sleep(1)
                    shutil.rmtree(x)

    def create_db(self, library_path):
        from calibre.library.database2 import LibraryDatabase2

        if LibraryDatabase2.exists_at(library_path):
            raise ValueError(f'A library already exists at {library_path!r}')
        src = os.path.join(os.path.dirname(__file__), 'metadata.db')
        dest = os.path.join(library_path, 'metadata.db')
        shutil.copyfile(src, dest)
        db = LibraryDatabase2(library_path)
        db.set_cover(1, I('lt.png', data=True))
        db.set_cover(2, I('polish.png', data=True))
        db.add_format(1, 'FMT1', BytesIO(b'book1fmt1'), index_is_id=True)
        db.add_format(1, 'FMT2', BytesIO(b'book1fmt2'), index_is_id=True)
        db.add_format(2, 'FMT1', BytesIO(b'book2fmt1'), index_is_id=True)
        db.conn.close()
        return dest

    def init_cache(self, library_path=None):
        from calibre.db.backend import DB
        from calibre.db.cache import Cache

        backend = DB(library_path or self.library_path)
        cache = Cache(backend)
        cache.init()
        self.objects_to_close.append(cache)
        return cache

    def assert_library_consistent(self, cache):
        """
        Check that the library on disk is consistent with the in-memory state
        of the specified Cache. Meant to be called by any test that modifies
        the library, after the modification, particularly after an operation
        that failed. It checks that:

        * A fresh Cache opened on the library from disk has exactly the same
          books, and the same value for every field of every book, same
          formats and same cover flag, as the live cache. Which means that
          the database and the in-memory state agree.
        * The folder, the listed format files and the cover of every book
          exist on disk.
        * There are no orphaned book folders, folders that do not belong to
          any book in the database.
        """
        import re

        from calibre.db.backend import DB
        from calibre.db.cache import Cache
        from calibre.db.constants import COVER_FILE_NAME, NOTES_DIR_NAME, TRASH_DIR_NAME

        def normalize(x):
            # The in-memory timestamps of newly created books have a
            # microsecond part that is not stored in the database
            return x.replace(microsecond=0) if isinstance(x, datetime) else x

        library_path = cache.backend.library_path
        # Hold the write lock so that the background threads of the live
        # cache, such as the one that counts pages, do not use the database
        # while the new connection is being set up
        with cache.quiet_write_lock:
            fresh = Cache(DB(library_path))
            fresh.init()
        try:
            ids = set(cache.all_book_ids())
            self.assertEqual(ids, set(fresh.all_book_ids()), 'The in-memory book ids differ from those in the database')
            for field in sorted(cache.fields):
                # ondevice is computed from the device, pages is -1 in memory
                # for a book that is queued for a pages scan, 0 in the database
                if field in ('ondevice', 'pages'):
                    continue
                for book_id in sorted(ids):
                    self.assertEqual(
                        normalize(cache.field_for(field, book_id)),
                        normalize(fresh.field_for(field, book_id)),
                        f'The field {field} of book {book_id} differs between the in-memory state and the database',
                    )
            for book_id in sorted(ids):
                formats = cache.formats(book_id, verify_formats=False)
                self.assertEqual(formats, fresh.formats(book_id, verify_formats=False), f'Formats of book {book_id} differ')
                has_cover = bool(cache.field_for('cover', book_id))
                path = cache.field_for('path', book_id)
                bdir = os.path.join(library_path, *path.split('/')) if path else ''
                if formats or has_cover:
                    self.assertTrue(bdir and os.path.isdir(bdir), f'The folder of book {book_id} is missing: {bdir!r}')
                for fmt in formats:
                    fpath = cache.format_abspath(book_id, fmt)
                    self.assertTrue(fpath and os.path.exists(fpath), f'The {fmt} file of book {book_id} is missing from the disk')
                    self.assertTrue(
                        os.path.exists(fresh.format_abspath(book_id, fmt) or ''), f'The {fmt} file of book {book_id} is missing from the disk'
                    )
                if has_cover:
                    self.assertTrue(os.path.exists(os.path.join(bdir, COVER_FILE_NAME)), f'The cover of book {book_id} is missing from the disk')
            # Orphaned folders
            expected = {os.path.join(*cache.field_for('path', x).split('/')) for x in ids if cache.field_for('path', x)}
            pat = re.compile(r'\((\d+)\)$')
            found = set()
            for author in os.listdir(library_path):
                apath = os.path.join(library_path, author)
                if author in (TRASH_DIR_NAME, NOTES_DIR_NAME) or not os.path.isdir(apath):
                    continue
                children = os.listdir(apath)
                self.assertTrue(children, f'Orphaned empty author folder: {author!r}')
                for title in children:
                    rel = os.path.join(author, title)
                    m = pat.search(title)
                    self.assertTrue(m is not None and os.path.isdir(os.path.join(library_path, rel)), f'Unexpected item in the library: {rel!r}')
                    found.add(rel)
            orphans = found - expected
            self.assertFalse(orphans, f'Orphaned book folders (no matching book in the database): {sorted(orphans)}')
        finally:
            fresh.close()

    def collect_book_files(self, cache):
        """
        Return the set of (book_id, name) for every format file and cover that
        exists anywhere in the library, either in a book folder or in the
        trash, whether or not the book is in the database. name is the upper
        cased format or COVER. Use with :meth:`assert_files_conserved`.
        """
        import re

        from calibre.db.constants import COVER_FILE_NAME, METADATA_FILE_NAME, TRASH_DIR_NAME

        library_path = cache.backend.library_path
        ans = set()
        pat = re.compile(r'\((\d+)\)$')

        def add_dir(book_id, path):
            for x in os.listdir(path):
                if x == COVER_FILE_NAME:
                    ans.add((book_id, 'COVER'))
                elif x != METADATA_FILE_NAME and os.path.isfile(os.path.join(path, x)) and '.' in x:
                    ans.add((book_id, x.rpartition('.')[2].upper()))

        for author in os.listdir(library_path):
            apath = os.path.join(library_path, author)
            if author.startswith('.') or not os.path.isdir(apath):
                continue
            for title in os.listdir(apath):
                m = pat.search(title)
                if m is not None and os.path.isdir(os.path.join(apath, title)):
                    add_dir(int(m.group(1)), os.path.join(apath, title))
        trash = os.path.join(library_path, TRASH_DIR_NAME)
        for sub in ('b', 'f'):
            base = os.path.join(trash, sub)
            if os.path.isdir(base):
                for book_id in os.listdir(base):
                    if book_id.isdigit() and os.path.isdir(os.path.join(base, book_id)):
                        if sub == 'b':
                            add_dir(int(book_id), os.path.join(base, book_id))
                        else:
                            for x in os.listdir(os.path.join(base, book_id)):
                                if x != 'metadata.json':
                                    ans.add((int(book_id), x.upper()))
        return ans

    def assert_files_conserved(self, before, cache, expected_lost=()):
        """
        Assert that no format file or cover was lost since the files were
        collected using :meth:`collect_book_files`, other than the specified
        expected_lost (an iterable of (book_id, name)).
        """
        after = self.collect_book_files(cache)
        lost = before - after - set(expected_lost)
        self.assertFalse(lost, f'Files were lost: {sorted(lost)}')

    def mkdtemp(self):
        ans = tempfile.mkdtemp(prefix='db_test_')
        self.paths_to_remove.append(ans)
        return ans

    def init_old(self, library_path=None):
        from calibre.library.database2 import LibraryDatabase2

        ans = LibraryDatabase2(library_path or self.library_path)
        self.objects_to_close.append(ans)
        return ans

    def init_legacy(self, library_path=None):
        from calibre.db.legacy import LibraryDatabase

        ans = LibraryDatabase(library_path or self.library_path)
        self.objects_to_close.append(ans)
        return ans

    def clone_library(self, library_path):
        if not hasattr(self, 'clone_dir'):
            self.clone_dir = tempfile.mkdtemp()
            self.paths_to_remove.append(self.clone_dir)
            self.clone_count = 0
        self.clone_count += 1
        dest = os.path.join(self.clone_dir, str(self.clone_count))
        shutil.copytree(library_path, dest)
        return dest

    @property
    def cloned_library(self):
        return self.clone_library(self.library_path)

    def compare_metadata(self, mi1, mi2, exclude=()):
        allfk1 = mi1.all_field_keys()
        allfk2 = mi2.all_field_keys()
        self.assertEqual(allfk1, allfk2)

        all_keys = {
            'format_metadata',
            'id',
            'application_id',
            'author_sort_map',
            'link_maps',
            'book_size',
            'ondevice_col',
            'last_modified',
            'has_cover',
            'cover_data',
        }.union(allfk1)
        for attr in all_keys:
            if attr in {'user_metadata', 'book_size', 'ondevice_col', 'db_approx_formats'} or attr in exclude:
                continue
            attr1, attr2 = getattr(mi1, attr), getattr(mi2, attr)
            if attr == 'formats':
                attr1, attr2 = (tuple(x) if x else () for x in (attr1, attr2))
            if isinstance(attr1, (tuple, list)) and 'authors' not in attr and 'languages' not in attr:
                attr1, attr2 = set(attr1), set(attr2)
            self.assertEqual(attr1, attr2, f'{attr} not the same: {attr1!r} != {attr2!r}')
            if attr.startswith('#') and attr + '_index' not in exclude:
                attr1, attr2 = mi1.get_extra(attr), mi2.get_extra(attr)
                self.assertEqual(attr1, attr2, f'{attr} {{#extra}} not the same: {attr1!r} != {attr2!r}')
