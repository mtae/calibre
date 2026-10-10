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
    'mechanize',  # Only needed for web metadata look ups, by the amazon source
    # Device drivers, AI providers and archive import plugins are loaded by
    # calibre.customize.ui._ensure_late() only when something asks for them
    'calibre.devices.kindle.driver',
    'calibre.devices.kobo.driver',
    'calibre.devices.smart_device_app.driver',
    'calibre.ai.openai',
    'calibre.ebooks.metadata.archive',
    'calibre.ebooks.unihandecode',  # Large tables; only needed for non-ASCII CSS class names, filenames, etc.
)


def tiny_epub(path):
    from calibre.ebooks.conversion.golden_test import make_epub

    chapters = [
        ('c1.xhtml', 'One', '<h1>One</h1><p class="a">Hello world.</p>'),
        ('c2.xhtml', 'Two', '<h1>Two</h1><p class="a">Second page.</p>'),
    ]
    make_epub(path, chapters, 'p.a{text-indent:1em}h1{font-size:2em}')


def convert_and_report_modules(tdir, dest_fmt, args=()):
    """Run in a separate process: convert a tiny book and return the names of all loaded modules"""
    import io
    import sys
    from contextlib import redirect_stderr, redirect_stdout

    from calibre.ebooks.conversion.cli import main
    from calibre.ebooks.conversion.golden_test import jpeg_bytes

    src = os.path.join(tdir, 'tiny.epub')
    tiny_epub(src)
    # A cover is passed so that no default cover is generated, as that needs Qt widgets
    cover = os.path.join(tdir, 'cover.jpg')
    with open(cover, 'wb') as f:
        f.write(jpeg_bytes())
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
