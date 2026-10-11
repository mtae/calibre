#!/usr/bin/env python
# License: GPLv3 Copyright: 2011, Kovid Goyal <kovid@kovidgoyal.net>

import random

from calibre.gui2.actions import InterfaceAction
from calibre.utils.localization import _


class PickRandomAction(InterfaceAction):
    name = 'Pick Random Book'
    action_spec = (_('Pick a random book'), 'random.png', _('Select a random book from your calibre library'), ())
    dont_add_to = frozenset(['context-menu-device'])

    def genesis(self):
        self.qaction.triggered.connect(self.pick_random)
        self.recently_picked = {}
        try:
            self.randint = random.SystemRandom().randint
        except Exception:
            self.randint = random.randint

    def location_selected(self, loc):
        enabled = loc == 'library'
        self.qaction.setEnabled(enabled)
        self.menuless_qaction.setEnabled(enabled)

    def library_changed(self, db):
        self.recently_picked = {}

    def pick_random(self):
        lv = self.gui.library_view
        count = lv.model().rowCount(None)
        if not count:
            return
        rp = self.recently_picked
        while len(rp) > count // 2:
            n = next(iter(rp))
            del rp[n]
        while True:
            pick = self.randint(0, count - 1)
            if pick in rp:
                continue
            rp[pick] = True
            break
        lv.set_current_row(pick)
        lv.scroll_to_row(pick)


def find_tests():
    import unittest
    from types import SimpleNamespace

    class TestPickRandom(unittest.TestCase):
        def pick(self, count, randint):
            rows = []
            lv = SimpleNamespace(
                model=lambda: SimpleNamespace(rowCount=lambda parent: count),
                set_current_row=rows.append,
                scroll_to_row=lambda row: None,
            )
            action = SimpleNamespace(gui=SimpleNamespace(library_view=lv), recently_picked={}, randint=randint)
            PickRandomAction.pick_random(action)
            return rows

        def test_pick_random_row_in_range(self):
            # randint() is inclusive, so ask for the largest value it can return
            for count in (1, 2, 5):
                self.assertEqual(self.pick(count, lambda lo, hi: hi), [count - 1])
            self.assertEqual(self.pick(0, lambda lo, hi: hi), [])

    return unittest.defaultTestLoader.loadTestsFromTestCase(TestPickRandom)
