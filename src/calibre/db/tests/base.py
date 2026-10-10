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

    def find_library_inconsistencies(self, cache):
        """
        Check that the library on disk is consistent with the in-memory state
        of the specified Cache and return a list of the problems found, as
        (kind, message) tuples. An empty list means that the library is
        consistent. It never raises an exception for an inconsistency, use
        :meth:`assert_library_consistent` for tests that expect none. The kinds
        are:

        * ``open``: A fresh Cache cannot be opened on the library
        * ``ids``: The set of book ids differs between the live cache and the database
        * ``field:<name>``: The value of the field for some book differs
        * ``composite-cache``: The same for composite columns, whose values are cached
        * ``items:<field>``: The items (tags, authors, ...) of a many-one/many-many field differ,
          or their links, or author sorts, ``formats``: the formats of a book differ
        * ``folder-missing``, ``format-file-missing``, ``cover-file-missing``: A book that
          has formats or a cover has no folder, or a listed file does not exist
        * ``format-size``: The size recorded for a format is not the size of the file
        * ``stray-cover``: A book without a cover has a cover file
        * ``orphan-folder``: A book folder or author folder that does not belong to any book
        * ``prefs``: A preference differs between the live cache and the database
        * ``note-orphan``, ``note-resource-missing``, ``note-backup-missing``: A note of an item that does not exist,
          a resource used by a note or the backup file of a note that does not exist on the disk
        * ``notes-differ``: The items that have notes differ between the live cache and the database
        """
        import re

        from calibre.db.backend import DB
        from calibre.db.cache import Cache
        from calibre.db.constants import COVER_FILE_NAME, NOTES_DIR_NAME, TRASH_DIR_NAME

        problems = []

        def add(kind, msg):
            problems.append((kind, msg))

        def normalize(x, field=None):
            # The in-memory timestamps of newly created books have a
            # microsecond part that is not stored in the database
            if isinstance(x, datetime):
                return x.replace(microsecond=0)
            # The order of formats depends on the order of the rows in the database
            if field == 'formats' and x:
                return tuple(sorted(x))
            # A book without formats has a size of 0 in memory and NULL in the database
            if field == 'size':
                return x or 0
            return x

        library_path = cache.backend.library_path
        # Hold the write lock so that the background threads of the live
        # cache, such as the one that counts pages, do not use the database
        # while the new connection is being set up
        try:
            with cache.quiet_write_lock:
                fresh = Cache(DB(library_path))
                fresh.init()
        except Exception as err:
            add('open', f'A fresh Cache cannot be opened on the library: {err!r}')
            return problems
        try:
            ids = set(cache.all_book_ids())
            if ids != set(fresh.all_book_ids()):
                add(
                    'ids',
                    f'The in-memory book ids differ from those in the database: only in memory {sorted(ids - set(fresh.all_book_ids()))},'
                    f' only in the database {sorted(set(fresh.all_book_ids()) - ids)}',
                )
            ids &= set(fresh.all_book_ids())
            for field in sorted(cache.fields):
                # ondevice is computed from the device, pages is -1 in memory
                # for a book that is queued for a pages scan, 0 in the database
                if field in ('ondevice', 'pages'):
                    continue
                for book_id in sorted(ids):
                    if normalize(cache.field_for(field, book_id), field) != normalize(fresh.field_for(field, book_id), field):
                        # The values of composite columns are cached, so they are stale
                        # when a change is not followed by clearing the cache
                        kind = 'composite-cache' if cache.fields[field].is_composite else f'field:{field}'
                        add(kind, f'The field {field} of book {book_id} differs between the in-memory state and the database')
                        break
                # Items of many-one and many-many fields
                table = getattr(cache.fields[field], 'table', None)
                ftable = getattr(fresh.fields[field], 'table', None) if field in fresh.fields else None
                for attr in ('id_map', 'link_map', 'asort_map'):
                    if hasattr(table, attr):
                        # Items without a link are not always in link_map
                        mine, theirs = getattr(table, attr), getattr(ftable, attr, None)
                        if attr == 'link_map':
                            mine, theirs = ({k: v for k, v in x.items() if v} for x in (mine, theirs or {}))
                        if mine != theirs:
                            add(f'items:{field}', f'The {attr} of {field} differs between the in-memory state and the database')
            for book_id in sorted(ids):
                formats = cache.formats(book_id, verify_formats=False)
                if sorted(formats) != sorted(fresh.formats(book_id, verify_formats=False)):
                    add('formats', f'Formats of book {book_id} differ')
                has_cover = bool(cache.field_for('cover', book_id))
                path = cache.field_for('path', book_id)
                bdir = os.path.join(library_path, *path.split('/')) if path else ''
                if (formats or has_cover) and not (bdir and os.path.isdir(bdir)):
                    add('folder-missing', f'The folder of book {book_id} is missing: {bdir!r}')
                    continue
                for fmt in formats:
                    fpath = cache.format_abspath(book_id, fmt)
                    if not (fpath and os.path.exists(fpath)):
                        add('format-file-missing', f'The {fmt} file of book {book_id} is missing from the disk')
                        continue
                    if not os.path.exists(fresh.format_abspath(book_id, fmt) or ''):
                        add('format-file-missing', f'The {fmt} file of book {book_id}, as found by a fresh cache, is missing from the disk')
                    elif cache.fields['formats'].table.size_map[book_id].get(fmt) != os.path.getsize(fpath):
                        add('format-size', f'The recorded size of the {fmt} file of book {book_id} is not the size of the file')
                cover = os.path.join(bdir, COVER_FILE_NAME) if bdir else ''
                if has_cover and not os.path.exists(cover):
                    add('cover-file-missing', f'The cover of book {book_id} is missing from the disk')
                elif not has_cover and cover and os.path.exists(cover):
                    add('stray-cover', f'Book {book_id} is marked as having no cover but has a cover file')
            # Orphaned folders
            expected = {os.path.join(*cache.field_for('path', x).split('/')) for x in ids if cache.field_for('path', x)}
            pat = re.compile(r'\((\d+)\)$')
            found = set()
            for author in os.listdir(library_path):
                apath = os.path.join(library_path, author)
                if author in (TRASH_DIR_NAME, NOTES_DIR_NAME) or not os.path.isdir(apath):
                    continue
                children = os.listdir(apath)
                if not children:
                    add('orphan-folder', f'Orphaned empty author folder: {author!r}')
                for title in children:
                    rel = os.path.join(author, title)
                    if pat.search(title) is None or not os.path.isdir(os.path.join(library_path, rel)):
                        add('orphan-folder', f'Unexpected item in the library: {rel!r}')
                    found.add(rel)
            orphans = found - expected
            if orphans:
                add('orphan-folder', f'Orphaned book folders (no matching book in the database): {sorted(orphans)}')
            # Preferences, they include the saved searches and virtual libraries
            if dict(cache.backend.prefs) != dict(fresh.backend.prefs):
                changed = sorted(k for k in set(cache.backend.prefs) | set(fresh.backend.prefs) if cache.backend.prefs.get(k) != fresh.backend.prefs.get(k))
                add('prefs', f'The preferences {changed} differ between the in-memory state and the database')
            # Notes
            try:
                notes = cache.get_all_items_that_have_notes()
                if notes != fresh.get_all_items_that_have_notes():
                    add('notes-differ', 'The items that have notes differ between the in-memory state and the database')
                for field, item_ids in notes.items():
                    for item_id in item_ids:
                        if item_id not in cache.fields[field].table.id_map:
                            add('note-orphan', f'The note for {field} item {item_id} belongs to an item that does not exist')
                            continue
                        data = cache.notes_data_for(field, item_id) or {}
                        for rhash in data.get('resource_hashes', ()):
                            if not os.path.exists(cache.backend.notes.path_for_resource(rhash)):
                                add('note-resource-missing', f'The resource {rhash} of the note for {field} item {item_id} is missing from the disk')
                        if not os.path.exists(os.path.join(cache.backend.notes.backup_dir, field, str(item_id))):
                            add('note-backup-missing', f'The backup file of the note for {field} item {item_id} is missing')
            except Exception as err:
                add('notes-differ', f'Could not check the notes: {err!r}')
        finally:
            fresh.close()
        return problems

    def assert_library_consistent(self, cache):
        """
        Check that the library on disk is consistent with the in-memory state
        of the specified Cache. Meant to be called by any test that modifies
        the library, after the modification, particularly after an operation
        that failed. It checks that:

        * A fresh Cache opened on the library from disk has exactly the same
          books, and the same value for every field of every book, same
          formats and same cover flag, as the live cache. Which means that
          the database and the in-memory state agree. The items of the many
          one and many-many fields (tags, authors, ...) are also compared.
        * The folder, the listed format files and the cover of every book
          exist on disk, and the sizes recorded for the formats are those of
          the files.
        * There are no orphaned book folders, folders that do not belong to
          any book in the database.
        * Notes refer to items and resources that exist.

        See :meth:`find_library_inconsistencies` for a non raising version.
        """
        problems = self.find_library_inconsistencies(cache)
        self.assertFalse(problems, 'The library is not consistent:\n' + '\n'.join(f'  {kind}: {msg}' for kind, msg in problems[:20]))

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
