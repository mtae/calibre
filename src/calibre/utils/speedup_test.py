#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import os
import sys
import tempfile
import threading
import unittest

from calibre_extensions import speedup


class TestSpeedup(unittest.TestCase):
    ae = unittest.TestCase.assertEqual

    def test_string_arguments(self):
        "Test that functions taking a str reject other types and embedded NUL characters"
        # These functions used to use the "s" format of PyArg_ParseTuple(),
        # which raises ValueError for embedded NULs. They must not silently
        # use the part of the string before the NUL.
        self.ae(speedup.parse_iso8601('2020-01-02T03:04:05Z')[0].isoformat(), '2020-01-02T03:04:05')
        self.assertIsNotNone(speedup.parse_date('2020-01-02 03:04:05+00:00'))
        for func in (speedup.parse_iso8601, speedup.parse_date):
            for bad in ('2020-01-02T03:04:05Z\0junk', '2020-01-02 03:04:05+00:00\0', '\0'):
                with self.assertRaises(ValueError, msg=f'{func.__name__}({bad!r}) did not raise a ValueError'):
                    func(bad)
            for bad in (b'2020-01-02T03:04:05Z', None, 1):
                with self.assertRaises(TypeError, msg=f'{func.__name__}({bad!r}) did not raise a TypeError'):
                    func(bad)
        errors = []

        def set_name():
            try:
                speedup.set_thread_name('calibre-test')
            except Exception as e:
                errors.append(e)
            try:
                speedup.set_thread_name('calibre\0test')
            except Exception as e:
                errors.append(e)
            else:
                errors.append('set_thread_name() accepted a name with an embedded NUL')

        if not sys.platform.startswith('win'):
            t = threading.Thread(target=set_name)
            t.start()
            t.join()
            self.assertEqual(len(errors), 1, errors)
            self.assertIsInstance(errors[0], ValueError)

    @unittest.skipIf(getattr(sys, 'frozen', False), 'The subprocess helper does not work in frozen builds')
    def test_detach_argument_errors(self):
        "Test that detach() validates its argument before redirecting the standard streams"
        # In a subprocess as redirecting the standard streams is a global side effect
        from calibre.utils.icu_test import run_in_subprocess

        code = f"""
import sys
from calibre_extensions import speedup
for bad in ({os.devnull!r} + '\\0junk', b{os.devnull!r}, None):
    try:
        speedup.detach(bad)
    except (ValueError, TypeError):
        pass
    else:
        print(f'detach({{bad!r}}) did not raise')
        raise SystemExit(1)
print('OK')
"""
        p = run_in_subprocess(code)
        self.ae((p.returncode, p.stdout.strip()), (0, 'OK'), f'returncode={p.returncode}\nstdout={p.stdout}\nstderr={p.stderr}')

    @unittest.skipIf(sys.platform.startswith('win'), 'pread_all() takes a C runtime file descriptor')
    def test_pread_all_arguments(self):
        "Test that pread_all() rejects file descriptors that do not fit in a C int instead of truncating them"
        data = os.urandom(1000)
        with tempfile.TemporaryFile() as f:
            f.write(data)
            f.flush()
            fd = f.fileno()
            self.ae(speedup.pread_all(fd, 10, 5), data[5:15])
            self.ae(speedup.pread_all(fd, 2000), data)
            for bad in (fd + 2**32, fd - 2**32, 2**31, -(2**31) - 1, 2**63):
                with self.assertRaises(OverflowError, msg=f'pread_all({bad}, 10) did not raise an OverflowError'):
                    speedup.pread_all(bad, 10)
            with self.assertRaises(OSError):
                speedup.pread_all(-1, 10)
            for bad in (1.5, '1', None):
                with self.assertRaises(TypeError):
                    speedup.pread_all(bad, 10)
            with self.assertRaises(TypeError):
                speedup.pread_all(fd)

    def test_other_converted_functions(self):
        "Test the argument handling of the other functions using METH_FASTCALL"
        self.ae(speedup.pdf_float(1.5), '1.5')
        self.ae(speedup.pdf_float(2), '2')
        with self.assertRaises(TypeError):
            speedup.pdf_float('1')
        with self.assertRaises(TypeError):
            speedup.pdf_float()
        data = bytearray(b'hello world')
        speedup.websocket_mask(data, b'\1\2\3\4')
        self.ae(bytes(data), bytes(c ^ b'\1\2\3\4'[i % 4] for i, c in enumerate(b'hello world')))
        data = bytearray(b'abcd')
        speedup.websocket_mask(data, b'\1\2\3\4', 1)
        self.ae(bytes(data), bytes(c ^ b'\1\2\3\4'[(i + 1) % 4] for i, c in enumerate(b'abcd')))
        with self.assertRaises(OverflowError):
            speedup.websocket_mask(bytearray(b'abcd'), b'\1\2\3\4', 2**64)
        with self.assertRaises(TypeError):
            speedup.websocket_mask(b'abcd')
        text, state, codep = speedup.utf8_decode('aé\U0001f431'.encode())
        self.ae((text, state), ('aé\U0001f431', 0))
        text, state, codep = speedup.utf8_decode('\U0001f431'.encode()[:2])
        self.ae(text, '')
        self.assertNotEqual(state, 0)
        text2, state2, codep2 = speedup.utf8_decode('\U0001f431'.encode()[2:], state, codep)
        self.ae((text2, state2), ('\U0001f431', 0))
        with self.assertRaises(ValueError):
            speedup.utf8_decode(b'\xff')
        self.ae(speedup.utf8_decode(b'a', 0)[0], 'a')
        for bad_state in (9, 100, 2**32 - 1, 2**32, 2**64 - 1):  # the decoder has states 0 to 8, a larger one indexes past its table
            with self.assertRaises(ValueError, msg=f'state={bad_state}'):
                speedup.utf8_decode(b'a', bad_state)

    @unittest.skipIf(getattr(sys, 'frozen', False), 'The subprocess helper does not work in frozen builds')
    def test_clean_xml_chars_empty_subclass(self):
        "Test that clean_xml_chars() returns a new reference when given an empty str subclass instance"
        # In a subprocess as dropping the result of the buggy code frees the argument while it is still in use
        code = '''
import sys
from calibre_extensions.speedup import clean_xml_chars
class S(str):
    pass
s = S('')
before = sys.getrefcount(s)
r = clean_xml_chars(s)
if r != '' or sys.getrefcount(s) != before + (r is s):
    print('refcount of the argument not incremented', before, sys.getrefcount(s))
    raise SystemExit(1)
del r
if sys.getrefcount(s) != before:
    print('refcount of the argument changed', before, sys.getrefcount(s))
    raise SystemExit(1)
print('OK')
'''
        p = run_in_subprocess(code)
        self.ae((p.returncode, p.stdout.strip()), (0, 'OK'), f'returncode={p.returncode}\nstdout={p.stdout}\nstderr={p.stderr}')


def find_tests():
    return unittest.defaultTestLoader.loadTestsFromTestCase(TestSpeedup)
