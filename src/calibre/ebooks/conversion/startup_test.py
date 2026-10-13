#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, calibre contributors

"""
Tests that keep the start up cost of a conversion down.

A conversion of even a tiny book spends a large part of its time importing
modules that it never uses. These tests run a conversion in a fresh process and
check which expensive modules were imported, so that someone adding a top level
import of, for example, bs4 to a module that every conversion imports, finds
out here instead of via slower conversions. If you have a good reason for
needing one of these modules during every conversion, remove it from
HEAVY_MODULES with a comment explaining why.
"""

import os
import unittest

# Modules that a conversion of a plain book to EPUB/MOBI/AZW3 must not import
HEAVY_MODULES = (
    'bs4',  # Only needed by library.comments for rarely used HTML comment conversion, via jacket
    'html5lib',
    'soupsieve',
)

CONTAINER = '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>'''

OPF = '''<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>Tiny</dc:title><dc:creator>An Author</dc:creator><dc:language>en</dc:language>
<dc:identifier id="id">tiny-startup-test</dc:identifier>
</metadata>
<manifest>
<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
<item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>
<item id="css" href="style.css" media-type="text/css"/>
</manifest>
<spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>'''

CHAPTER = '''<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{0}</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body><h1>{0}</h1><p class="a">Hello world.</p><p class="b">Second paragraph.</p></body></html>'''

# A 1x1 PNG
COVER = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90'
    b'wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\r\xefF\xb8\x00\x00\x00\x00IEND\xaeB`\x82'
)


def tiny_epub(path):
    from zipfile import ZIP_STORED, ZipFile

    with ZipFile(path, 'w') as zf:
        zf.writestr('mimetype', 'application/epub+zip', compress_type=ZIP_STORED)
        zf.writestr('META-INF/container.xml', CONTAINER)
        zf.writestr('content.opf', OPF)
        zf.writestr('c1.xhtml', CHAPTER.format('One'))
        zf.writestr('c2.xhtml', CHAPTER.format('Two'))
        zf.writestr('style.css', 'p.a{text-indent:1em}p.b{font-weight:bold}h1{font-size:2em}')


def convert_and_report_modules(tdir, dest_fmt, args=()):
    """Run in a separate process: convert a tiny book and return the names of all loaded modules"""
    import io
    import sys
    from contextlib import redirect_stderr, redirect_stdout

    from calibre.ebooks.conversion.cli import main

    src = os.path.join(tdir, 'tiny.epub')
    tiny_epub(src)
    # A cover is passed so that no default cover is generated, as that needs Qt widgets
    cover = os.path.join(tdir, 'cover.png')
    with open(cover, 'wb') as f:
        f.write(COVER)
    dest = os.path.join(tdir, 'out.' + dest_fmt)
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        ret = main(['ebook-convert', src, dest, '--cover', cover, *args])
    if ret or not os.path.getsize(dest):
        raise AssertionError(f'Conversion failed: {out.getvalue()}')
    return sorted(sys.modules)


def find_tests():
    from calibre.ptempfile import TemporaryDirectory
    from calibre.utils.ipc.simple_worker import fork_job

    class StartupTest(unittest.TestCase):
        def loaded_modules(self, fmt, args=()):
            with TemporaryDirectory('_startup') as tdir:
                r = fork_job(
                    'calibre.ebooks.conversion.startup_test',
                    'convert_and_report_modules',
                    args=(tdir, fmt, args),
                    no_output=True,
                    timeout=240,
                )
                return frozenset(r['result'])

        def test_heavy_modules_not_imported(self):
            for fmt in ('mobi', 'epub', 'azw3'):
                loaded = self.loaded_modules(fmt)
                for mod in HEAVY_MODULES:
                    self.assertTrue(mod not in loaded, f'{mod} was imported during a conversion to {fmt}')

    return unittest.defaultTestLoader.loadTestsFromTestCase(StartupTest)
