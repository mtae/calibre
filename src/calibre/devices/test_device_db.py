#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import sqlite3
import unittest
from datetime import datetime


class TestKoboDB(unittest.TestCase):
    def test_set_readstatus_stores_valid_date(self):
        from calibre.devices.kobo.driver import KOBO

        c = sqlite3.connect(':memory:')
        c.row_factory = sqlite3.Row
        c.execute('create table content (ContentID text, BookID text, ReadStatus int, FirstTimeReading text, DateLastRead text)')
        c.execute("insert into content values ('book1', null, 0, 'true', null)")
        KOBO.set_readstatus(None, c, 'book1', 1)
        status, date = c.execute('select ReadStatus, DateLastRead from content').fetchone()
        self.assertEqual(status, 1)
        # The date must be an actual timestamp, not the text of an SQL keyword
        datetime.strptime(date, '%Y-%m-%dT%H:%M:%S%z')

    def test_books_only_lists_books_from_requested_location(self):
        import contextlib
        import os
        import tempfile
        import types

        from calibre.devices.kobo.driver import KOBO

        c = sqlite3.connect(':memory:')
        c.row_factory = sqlite3.Row
        c.execute(
            'create table content (Title text, Attribution text, DateCreated text, ContentID text, MimeType text, ContentType text,'
            ' ImageID text, ReadStatus int, ___ExpirationStatus int, FavouritesIndex int, Accessibility int, IsDownloaded int, BookID text)'
        )
        c.executemany(
            "insert into content values (?,'A','2020-01-01T00:00:00',?,'application/epub+zip','6','img',0,1,-1,-1,1,null)",
            [('Main', 'file:///mnt/onboard/m.txt'), ('Card', 'file:///mnt/sd/books/x.txt')],
        )

        @contextlib.contextmanager
        def transaction(use_row_factory=False):
            yield c

        with tempfile.TemporaryDirectory() as tdir:
            d = KOBO.__new__(KOBO)
            d._main_prefix, d._card_a_prefix, d._card_b_prefix = (os.path.join(tdir, x) + os.sep for x in ('kobo', 'sd', 'sdb'))
            for name in ('kobo/m.txt', 'sd/books/x.txt'):
                os.makedirs(os.path.dirname(os.path.join(tdir, name)), exist_ok=True)
                open(os.path.join(tdir, name), 'wb').close()
            d.MUST_READ_METADATA = False
            d.dbversion = 33
            d.get_firmware_version = lambda: (2, 0)
            d.parse_metadata_cache = lambda *a: False
            d.settings = lambda: types.SimpleNamespace(extra_customization=[False] * 40, read_metadata=False)
            d.report_progress = d.sync_booklists = lambda *a: None
            d.database_transaction = transaction
            self.assertEqual([(b.title, b.lpath) for b in d.books(oncard='carda')], [('Card', 'books/x.txt')])
            self.assertEqual([(b.title, b.lpath) for b in d.books()], [('Main', 'm.txt')])

    def make_bookmark(self, contents, bookmarks, book_id, book_format, extension):
        import os

        from calibre.devices.kobo.bookmark import Bookmark

        c = sqlite3.connect(':memory:')
        c.row_factory = sqlite3.Row
        c.execute(
            'create table Content (ContentID text, BookTitle text, Title text, VolumeIndex int, ___NumPages int, MimeType text,'
            ' ContentType int, BookID text, DateLastRead text, ___PercentRead int, ReadStatus int)'
        )
        c.execute(
            'create table Bookmark (BookmarkID text, ContentID text, Text text, Annotation text, ChapterProgress real,'
            ' StartContainerChildIndex int, StartOffset int, Hidden text, VolumeID text)'
        )
        c.executemany("insert into Content values (?,?,?,?,1,'x',?,?,null,0,0)", contents)
        c.executemany("insert into Bookmark values (?,?,?,'',?,5,5,'false',?)", bookmarks)
        return Bookmark(c, book_id, os.path.abspath(__file__), 1, book_format, extension)

    def test_bookmark_ids_unique_across_unmatched_kepub_files(self):
        # Highlights in spine files that have no TOC entry all map to chapter 0 and are
        # interleaved with those of a file that does have one
        book = 'bookid'
        contents = [('bookid!OEBPS!Text/b.xhtml-1', 't', 'B', 3, 899, book)]
        bookmarks = [
            (f'{f}{i}', f'bookid!OEBPS!Text/{f}.xhtml', f'hl {f}{i}', 0.1 * (i + 1), book) for f, count in (('a', 3), ('b', 2), ('c', 3)) for i in range(count)
        ]
        bm = self.make_bookmark(contents, bookmarks, book, 'kepub', '.kepub')
        self.assertEqual(sorted(n['text'] for n in bm.user_notes.values()), sorted(b[2] for b in bookmarks))

    def test_bookmark_ids_unique_with_many_bookmarks_in_chapter(self):
        book = 'file:///mnt/onboard/a.epub'
        contents = [(f'c{i}', 't', f'ch{i}', i, 9, book) for i in range(2)]
        bookmarks = [(f'b{i}', 'c0', f'hl {i}', i / 2000, book) for i in range(1001)]
        bookmarks.append(('x', 'c1', 'chapter 1 hl', 0.1, book))
        bm = self.make_bookmark(contents, bookmarks, book, 'epub', '.epub')
        self.assertEqual(len(bm.user_notes), len(bookmarks))


class TestPaladinDB(unittest.TestCase):
    def test_read_device_books_fixes_booktags_book_id(self):
        from calibre.devices.paladin.driver import PALADIN

        c = sqlite3.connect(':memory:')
        c.executescript('''
            create table books (_id integer primary key autoincrement, filename text);
            create table tags (_id integer primary key autoincrement, tagname text);
            create table booktags (_id integer primary key autoincrement, tag_id integer, book_id integer);
            insert into books (_id, filename) values (5, 'a.epub');
            insert into tags (_id, tagname) values (1, 'Coll');
            insert into booktags (tag_id, book_id) values (1, 5);
        ''')
        # source_id 1 (SD card) with a book id below the minimum forces the ids to be renumbered
        PALADIN.read_device_books(PALADIN.__new__(PALADIN), c, 1, 'x')
        new_id = c.execute("select _id from books where filename = 'a.epub'").fetchone()[0]
        self.assertGreater(new_id, 5)
        self.assertEqual(c.execute('select tag_id, book_id from booktags').fetchall(), [(1, new_id)])


class TestSmartDeviceApp(unittest.TestCase):
    def test_get_file_stops_when_device_closes_connection(self):
        import socket
        import threading
        import types

        from calibre.devices.errors import ControlError
        from calibre.devices.smart_device_app.driver import SMART_DEVICE_APP

        writes = []

        def write(data):
            writes.append(data)
            self.assertLess(len(writes), 100, 'get_file keeps reading from a closed connection')

        d = SMART_DEVICE_APP.__new__(SMART_DEVICE_APP)
        d.sync_lock = threading.RLock()
        d.settings = lambda: types.SimpleNamespace(extra_customization=[False] * 20)
        d._debug = lambda *a, **k: None
        d._call_client = lambda *a, **k: ('OK', {'fileLength': 100})
        d._close_device_socket = lambda: None
        d.max_book_packet_len = 4096
        device, peer = socket.socketpair()
        peer.sendall(b'x' * 10)
        peer.close()
        d.device_socket = device
        try:
            self.assertRaises(ControlError, d.get_file, 'x.epub', types.SimpleNamespace(write=write))
        finally:
            device.close()


def find_tests():
    ans = unittest.defaultTestLoader.loadTestsFromTestCase(TestKoboDB)
    ans.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPaladinDB))
    ans.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestSmartDeviceApp))
    return ans
