#!/usr/bin/env python
# License: GPLv3

"""
Test the CLI of the calibre database management tool
"""

import csv
import os
import unittest

from calibre.db.cli.cmd_check_library import _print_check_library_results
from polyglot.io import PolyglotBytesIO


class Checker:
    def __init__(self, kw):
        for k, v in kw.items():
            setattr(self, k, v)


class PrintCheckLibraryResultsTest(unittest.TestCase):
    """
    Asserts the format of the output to the CLI to avoid regressions
    """

    check = ('dummy_check', 'Dummy Check')

    def test_prints_nothing_if_no_errors(self):
        stdout = PolyglotBytesIO()
        checker = Checker(dict.fromkeys(self.check))
        _print_check_library_results(checker, self.check, as_csv=False, out=stdout)
        self.assertEqual(stdout.getvalue(), b'')
        _print_check_library_results(checker, self.check, as_csv=True, out=stdout)
        self.assertEqual(stdout.getvalue(), b'')

    def test_human_readable_output(self):
        """
        Basic check of the human-readable output.

        Does not test: the full line format, truncation
        """
        data = [['first', 'second']]
        checker = Checker(dict.fromkeys(self.check))
        setattr(checker, self.check[0], data)
        stdout = PolyglotBytesIO()
        _print_check_library_results(checker, self.check, out=stdout, as_csv=False)

        result = stdout.getvalue().decode('utf-8', 'replace').split('\n')
        self.assertEqual(len(result), len(data) + 2)
        self.assertEqual(result[0], self.check[1])

        result_first = result[1].split('-')[0].strip()
        result_second = result[1].split('-')[1].strip()

        self.assertEqual(result_first, 'first')
        self.assertEqual(result_second, 'second')

        self.assertEqual(result[-1], '')

    def test_basic_csv_output(self):
        """
        Test simple csv output
        """
        data = [['first', 'second']]
        checker = Checker(dict.fromkeys(self.check))
        setattr(checker, self.check[0], data)
        stdout = PolyglotBytesIO()
        _print_check_library_results(checker, self.check, as_csv=True, out=stdout)

        result = stdout.getvalue().decode('utf-8', 'replace').split('\n')
        parsed_result = [l for l in csv.reader(result) if l]
        self.assertEqual(parsed_result, [[self.check[1], data[0][0], data[0][1]]])

    def test_escaped_csv_output(self):
        """
        Test more complex csv output
        """
        data = [['I, Caesar', 'second']]
        checker = Checker(dict.fromkeys(self.check))
        setattr(checker, self.check[0], data)
        stdout = PolyglotBytesIO()
        _print_check_library_results(checker, self.check, as_csv=True, out=stdout)

        result = stdout.getvalue().decode('utf-8', 'replace').split('\n')
        parsed_result = [l for l in csv.reader(result) if l]
        self.assertEqual(parsed_result, [[self.check[1], data[0][0], data[0][1]]])


class ExportTest(unittest.TestCase):
    def test_export_same_title_and_author(self):
        from calibre.ebooks.metadata.book.base import Metadata
        from calibre.library.save_to_disk import do_save_book_to_disk, sanitize_args
        from calibre.ptempfile import TemporaryDirectory

        class DB:
            def copy_format_to(self, book_id, fmt, path):
                with open(path, 'w') as f:
                    f.write(str(book_id))

        with TemporaryDirectory() as tdir:
            dest, opts, length = sanitize_args(tdir, None)
            opts.save_cover = opts.write_opf = opts.update_metadata = False
            used = set()
            for book_id in (1, 2, 3):
                mi = Metadata('Title', ['Author'])
                do_save_book_to_disk(DB(), book_id, mi, {}, ['TXT'], dest, opts, length, used_components=used)
            found = {}
            for dirpath, _dirs, files in os.walk(tdir):
                for name in files:
                    with open(os.path.join(dirpath, name)) as f:
                        found[os.path.relpath(os.path.join(dirpath, name), tdir)] = f.read()
            self.assertEqual(sorted(found.values()), ['1', '2', '3'])
            self.assertEqual(len({os.path.dirname(x) for x in found}), 1)


def find_tests():
    ans = unittest.defaultTestLoader.loadTestsFromTestCase(PrintCheckLibraryResultsTest)
    ans.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(ExportTest))
    return ans
