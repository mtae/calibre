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


def find_tests():
    ans = unittest.defaultTestLoader.loadTestsFromTestCase(TestKoboDB)
    ans.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPaladinDB))
    return ans
