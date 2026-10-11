#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import os
import tempfile
import unittest
from types import SimpleNamespace

from calibre.customize.profiles import OutputProfile
from calibre.utils.logging import default_log
from calibre.web.feeds.news import BasicNewsRecipe


class NoUrlRecipe(BasicNewsRecipe):
    title = 'No URL'
    calibre_most_common_ua = 'Mozilla/5.0'

    def resolve_masthead(self):
        self.masthead_path = None

    def resolve_cover(self):
        self.cover_path = None

    def create_opf(self, feeds, dir=None):
        pass  # needs the localization resources, which are not under test

    def parse_index(self):
        return [('Section', [{'title': 'No url', 'description': 'x'}])]

    def print_version(self, url):
        return url.replace('http:', 'https:')


class TestFeeds(unittest.TestCase):

    def test_print_version_failure_with_no_url(self):
        opts = SimpleNamespace(
            verbose=0, test=False, username=None, password=None, lrf=False, dont_download_recipe=True,
            debug_pipeline=None, recipe_specific_option=[], timeout=5, delay=0, max_recursions=1,
            no_stylesheets=False, from_opf=None,
            output_profile=OutputProfile,
        )
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tdir:
            os.chdir(tdir)
            try:
                recipe = NoUrlRecipe(opts, default_log, lambda *a, **kw: None)
                # An article without a url must be skipped, not abort the download
                recipe.build_index()
                self.assertFalse(recipe.jobs)
            finally:
                os.chdir(cwd)


def find_tests() -> unittest.TestSuite:
    return unittest.defaultTestLoader.loadTestsFromTestCase(TestFeeds)
