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


def find_tests():
    return unittest.defaultTestLoader.loadTestsFromTestCase(TestKoboDB)
