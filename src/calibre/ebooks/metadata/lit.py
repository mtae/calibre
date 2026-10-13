# License: GPLv3 Copyright: 2008, Kovid Goyal <kovid at kovidgoyal.net>

"""
Support for reading the metadata from a LIT file.
"""

import io
import os

from calibre.ebooks.metadata.opf2 import OPF


def get_metadata(stream):
    from calibre.ebooks.lit.reader import LitContainer
    from calibre.utils.logging import Log

    litfile = LitContainer(stream, Log())
    src = litfile.get_metadata().encode('utf-8')
    litfile = litfile._litfile
    opf = OPF(io.BytesIO(src), os.getcwd())
    mi = opf.to_book_metadata()
    covers = []
    for item in opf.iterguide():
        if 'cover' not in item.get('type', '').lower():
            continue
        ctype = item.get('type')
        href = item.get('href', '')
        candidates = [href, href.replace('&', '%26')]
        for item in litfile.manifest.values():
            if item.path in candidates:
                try:
                    covers.append((litfile.get_file('/data/' + item.internal), ctype))
                except Exception:
                    pass
                break
    covers.sort(key=lambda x: len(x[0]), reverse=True)
    idx = 0
    if len(covers) > 1:
        if covers[1][1] == covers[0][1] + '-standard':
            idx = 1
    if covers:
        mi.cover_data = ('jpg', covers[idx][0])
    return mi


def find_tests():
    import unittest
    from types import SimpleNamespace
    from unittest.mock import patch

    opf = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:title>Test</dc:title><dc:creator>An Author</dc:creator></metadata><manifest/><spine/><guide>%s</guide></package>'
    )

    class Container:
        def __init__(self, stream, log):
            manifest = {'a': SimpleNamespace(path='cover.jpg', internal='c1')}
            self._litfile = SimpleNamespace(manifest=manifest, get_file=lambda name: b'cover data')

    class Test(unittest.TestCase):
        def read_metadata(self, guide):
            Container.get_metadata = lambda self: opf % guide
            with patch('calibre.ebooks.lit.reader.LitContainer', Container):
                return get_metadata(None)

        def test_lit_metadata_without_cover(self):
            for guide in ('', '<reference type="cover" href="missing.jpg"/>'):
                mi = self.read_metadata(guide)
                self.assertEqual(mi.title, 'Test')
                self.assertEqual(mi.authors, ['An Author'])
                self.assertFalse(mi.cover_data[1])
            mi = self.read_metadata('<reference type="cover" href="cover.jpg"/>')
            self.assertEqual(mi.cover_data, ('jpg', b'cover data'))

    return unittest.defaultTestLoader.loadTestsFromTestCase(Test)
