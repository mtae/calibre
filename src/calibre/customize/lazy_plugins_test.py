#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, calibre contributors

"""
Tests for the lazily loaded builtin plugins (device drivers, AI providers,
archive import plugins): see calibre.customize.builtins_late and
calibre.customize.ui._ensure_late(). The plugins must be exactly the ones, in
exactly the order, that importing everything up front gives.
"""

import unittest

LATE_MODULES = (
    'calibre.devices.kindle.driver',
    'calibre.devices.kobo.driver',
    'calibre.devices.smart_device_app.driver',
    'calibre.ai.openai',
    'calibre.ebooks.metadata.archive',
)


def probe_lazy_state():
    """Run in a separate process"""
    import importlib
    import sys

    ui = importlib.import_module('calibre.customize.ui')
    ans = {}
    ans['loaded_at_start'] = [m for m in LATE_MODULES if m in sys.modules]
    # Things a conversion uses must not need the late plugins
    ui.plugin_for_input_format('epub')
    ui.plugin_for_output_format('mobi')
    list(ui.input_profiles()), list(ui.output_profiles())
    ui.metadata_readers()
    list(ui.plugins_for_ft('epub', 'preprocess')), list(ui.plugins_for_ft('epub', 'postprocess'))
    ui.find_plugin('MOBI Output')
    ans['loaded_after_conversion_calls'] = [m for m in LATE_MODULES if m in sys.modules]
    early = [p.name for p in ui._initialized_plugins]
    devices = [p.name for p in ui.device_plugins(include_disabled=True)]
    ans['loaded_after_device_plugins'] = [m for m in LATE_MODULES if m in sys.modules]
    ans['early'] = early
    ans['full'] = [p.name for p in ui.initialized_plugins()]
    ans['devices'] = devices
    return ans


def find_tests():
    from calibre.customize import ui
    from calibre.customize.builtins import LATE_NAMES, late_group, late_group_positions, plugin_slots
    from calibre.utils.ipc.simple_worker import fork_job

    def expected_classes():
        # What eager registration gave: every group expanded in place
        ans = []
        for x in plugin_slots:
            ans.extend(late_group(x) if isinstance(x, str) else (x,))
        return ans

    class LazyPluginsTest(unittest.TestCase):
        def test_late_names(self):
            names = [c.name for g in late_group_positions for c in late_group(g)]
            self.assertEqual(len(names), len(set(names)), 'duplicate plugin name')
            self.assertEqual(set(names), set(LATE_NAMES), 'LATE_NAMES in builtins.py is out of date')
            self.assertTrue(LATE_NAMES <= ui.builtin_names)

        def test_order_equals_eager_registration(self):
            classes = expected_classes()
            expected = sorted(classes, key=lambda c: c.priority, reverse=True)  # same stable sort as initialize_plugins()
            self.assertEqual(
                [(p.name, type(p)) for p in ui.initialized_plugins() if p.installation_type is ui.PluginInstallationType.BUILTIN],
                [(c.name, c) for c in expected],
            )

        def test_late_import_only_on_demand(self):
            r = fork_job('calibre.customize.lazy_plugins_test', 'probe_lazy_state', no_output=True, timeout=240)['result']
            self.assertEqual(r['loaded_at_start'], [], 'late modules imported by importing calibre.customize.ui')
            self.assertEqual(r['loaded_after_conversion_calls'], [], 'a call used by conversions loaded the late plugins')
            self.assertEqual(sorted(r['loaded_after_device_plugins']), sorted(LATE_MODULES))
            self.assertEqual(len(r['full']), len(r['early']) + len(LATE_NAMES))
            self.assertEqual([n for n in r['full'] if n not in LATE_NAMES], r['early'], 'order of the early plugins changed')
            self.assertEqual(r['full'], [p.name for p in ui.initialized_plugins()], 'lazy order differs from eager order')

        def test_failing_group_does_not_block_the_others(self):
            import io
            import sys

            real = ui.late_group
            out, err = sys.stdout, sys.stderr
            captured = io.StringIO()

            def bad_group(name):
                if name == 'devices':
                    sys.stdout = io.StringIO()  # what a misbehaving import may leave behind
                    raise ImportError('injected failure')
                return real(name)

            try:
                ui.initialize_plugins()  # all late groups pending again
                ui.late_group = bad_group
                sys.stderr = captured
                try:
                    ui._ensure_late()
                finally:
                    sys.stderr = err
                self.assertIs(sys.stdout, out, 'stdout not restored after a failing group')
                names = {p.name for p in ui._initialized_plugins}
                self.assertTrue({c.name for c in real('ai')} <= names, 'AI providers lost because the devices group failed')
                self.assertTrue({c.name for c in real('archive')} <= names, 'archive plugins lost because the devices group failed')
                self.assertFalse({c.name for c in real('devices')} & names)
                self.assertIn('injected failure', captured.getvalue())
            finally:
                ui.late_group = real
                sys.stdout, sys.stderr = out, err
                ui.initialize_plugins()
                ui._ensure_late()

    return unittest.defaultTestLoader.loadTestsFromTestCase(LazyPluginsTest)
