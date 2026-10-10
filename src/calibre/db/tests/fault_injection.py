#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

"""
Machinery for testing that the write methods of the Cache leave the library
consistent, both when they work and when they fail half way.

A :class:`Case` describes one operation on a prepared library (see
:func:`build_template`). :func:`run_case` runs it normally, recording every
write to the database and every file system operation it performs (the
"trace"). It then runs it again, once for every step in the trace, with a
failure injected at that step, and collects what is wrong with the library
afterwards. A test module is just a list of cases, see
:class:`FaultInjectionTest`.

Failures are injected by monkeypatching, only in the thread running the
operation:

* File operations: the helpers of the backend that perform them, see
  :func:`file_hook_targets`. The failure is raised either before the helper does
  its work or after it did it (``/after`` in the fault id), which leaves the
  file system changed but the caller unaware of it.
* SQL: the execution trace of the connection, which SQLite calls before every
  statement, for statements that write (INSERT, UPDATE, DELETE, REPLACE).
  Statements are identified by their text, the fault id is the text and which
  occurrence of it in the trace failed (``@1`` the first). The statement is not executed.

After a failure a library must be consistent
(:meth:`BaseTest.find_library_inconsistencies`), have lost no file
(:func:`lost_files`) and a retry of the operation must also leave it
consistent. Failures for which this is known not to hold are listed, with a
description of what is left inconsistent, in KNOWN_INCONSISTENT of the test
module, and the test fails if the actual set of problems is not exactly the
documented one. That makes the module both a regression test and a record of
the current weaknesses: when a method is fixed its entries must be removed.
"""

import atexit
import errno
import fnmatch
import hashlib
import os
import re
import shutil
import tempfile
import threading
import unittest
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from io import BytesIO
from typing import Any

import apsw

from calibre.constants import iswindows
from calibre.db.tests.base import IMG, BaseTest

# Ids of the books of the library built by build_template()
A, B, C, D = 1, 2, 3, 4  # D is in the trash

WRITE_SQL = re.compile(r'\s*(insert|update|delete|replace)\b', re.I)
# Book keeping for counting pages, which does not matter here as pages are not compared
IGNORED_SQL = re.compile(r'\bbooks_pages_link\b', re.I)
INJECTED = 'injected failure'
# Set the environment variable to a path to get the table of all the outcomes written there, see write_table()
TABLE_PATH = os.environ.get('CALIBRE_WRITE_CONSISTENCY_TABLE')
COLLECT_TABLE = bool(TABLE_PATH)


def stop_page_counting(cache):
    """Stop the cache from queueing books to have their pages counted by a background thread, which writes to the
    database at random times, making the order of writes unpredictable and logging errors for books that were removed"""
    cache.maintain_page_counts.queue_scan = lambda *a, **kw: None


# The library the cases operate on {{{
def build_template(test, path):
    """
    Create a library in path (an empty directory) containing:

    * Book A (1): title 'Title Two', authors Author Two & Author One, formats FMT1 and FMT2, a cover,
      tags t1 and t2, series 'Series One', extra files notes.txt and data/x.bin
    * Book B (2): title 'Title One', author Author One, format FMT1, a cover, tag t2, series 'Series One',
      the extra file extra2.txt
    * Book C (3): nothing but a title and an author
    * Book D (4): a deleted book with FMT1, FMT2, a cover and an extra file, in the trash
    * The format FMT3 of book B, in the trash
    * A note with a resource for the tag t1 and a note without resources for the author Author One
    """
    from calibre.db.backend import DB
    from calibre.db.cache import Cache
    from calibre.ebooks.metadata.book.base import Metadata

    test.create_db(path)
    cache = Cache(DB(path))
    cache.init()
    stop_page_counting(cache)
    try:
        cache.set_field('tags', {A: ['t1', 't2'], B: ['t2'], C: ['t3']})
        cache.set_field('series', {A: 'Series One', B: 'Series One'})
        cache.set_field('series_index', {A: 1, B: 2})
        cache.set_field('publisher', {A: 'Publisher One'})
        cache.set_field('#tags', {A: ['ct1', 'ct2']})
        cache.add_extra_files(A, {'notes.txt': BytesIO(b'extra notes'), 'data/x.bin': BytesIO(b'xbin')})
        cache.add_extra_files(B, {'extra2.txt': BytesIO(b'extra two')})
        book_id = cache.create_book_entry(Metadata('Trashed Book', ['Trash Author']), cover=IMG)
        assert book_id == D, book_id
        cache.add_format(D, 'FMT1', BytesIO(b'trashed fmt1'))
        cache.add_format(D, 'FMT2', BytesIO(b'trashed fmt2'))
        cache.add_extra_files(D, {'trashed.txt': BytesIO(b'trashed extra')})
        cache.remove_books([D])
        cache.add_format(B, 'FMT3', BytesIO(b'trashed fmt3'))
        cache.remove_formats({B: ['FMT3']})
        resource = cache.add_notes_resource(b'resource bytes', 'res.bin')
        cache.set_notes_for('tags', cache.get_item_id('tags', 't1'), '<p>note one</p>', resource_hashes=(resource,))
        cache.set_notes_for('authors', cache.get_item_id('authors', 'Author One'), '<p>author note</p>')
    finally:
        cache.close()
    return path


_template = []


def template_library(test):
    """Path of a library built once per process by build_template()"""
    if not _template:
        path = tempfile.mkdtemp(prefix='db_fault_template_')
        atexit.register(shutil.rmtree, path, ignore_errors=True)
        build_template(test, path)
        _template.append(path)
    return _template[0]


# }}}


# Files in the library {{{
def snapshot(cache):
    """
    The set of (book_id, name, sha1 of contents) of all files that belong to books, wherever
    they are in the library, including the trash. name is the upper cased format (all the formats
    used in the tests are FMT1, FMT2, ORIGINAL_FMT1, ...), COVER, or EXTRA for the other files (the
    extra files, whose names are not used as they can be renamed). The files of notes have
    book_id 0 and name notes/<path relative to the notes folder> for resources and retired notes.
    The names of the files of formats are not used either, as they change when a book is renamed.
    """
    from calibre.db.constants import COVER_FILE_NAME, METADATA_FILE_NAME, NOTES_DIR_NAME, TRASH_DIR_NAME

    library_path = cache.backend.library_path
    pat = re.compile(r'\((\d+)\)$')
    format_pat = re.compile(r'\.(original_)?fmt\d$', re.I)
    ans = set()

    def sha(path):
        with open(path, 'rb') as f:
            return hashlib.sha1(f.read()).hexdigest()

    def add_dir(book_id, path):
        for dirpath, _dirnames, filenames in os.walk(path):
            for x in filenames:
                full = os.path.join(dirpath, x)
                rel = os.path.relpath(full, path).replace(os.sep, '/')
                if rel == METADATA_FILE_NAME:
                    continue
                if rel == COVER_FILE_NAME:
                    name = 'COVER'
                elif '/' not in rel and format_pat.search(rel):
                    name = rel.rpartition('.')[2].upper()
                else:
                    name = 'EXTRA'
                ans.add((book_id, name, sha(full)))

    for author in os.listdir(library_path):
        apath = os.path.join(library_path, author)
        if author.startswith('.') or author == TRASH_DIR_NAME or not os.path.isdir(apath):
            continue
        for title in os.listdir(apath):
            m = pat.search(title)
            if m is not None and os.path.isdir(os.path.join(apath, title)):
                add_dir(int(m.group(1)), os.path.join(apath, title))
    trash = os.path.join(library_path, TRASH_DIR_NAME)
    for sub in ('b', 'f'):
        base = os.path.join(trash, sub)
        if os.path.isdir(base):
            for book_id in os.listdir(base):
                path = os.path.join(base, book_id)
                if not (book_id.isdigit() and os.path.isdir(path)):
                    continue
                if sub == 'b':
                    add_dir(int(book_id), path)
                else:
                    for x in os.listdir(path):
                        if x != 'metadata.json':
                            ans.add((int(book_id), x.upper(), sha(os.path.join(path, x))))
    notes = os.path.join(library_path, NOTES_DIR_NAME)
    for sub in ('resources', 'retired'):
        for dirpath, _dirnames, filenames in os.walk(os.path.join(notes, sub)):
            for x in filenames:
                full = os.path.join(dirpath, x)
                ans.add((0, 'notes/' + os.path.relpath(full, notes).replace(os.sep, '/'), sha(full)))
    return frozenset(ans)


def lost_files(before, after, allowed=()):
    """
    The (book_id, name, sha1) in before that are not in after, except for those whose
    book_id and name match one of allowed, a collection of (book_id, name) in which
    name can end with a * to match any name that starts with the rest of it.
    """

    def is_allowed(book_id, name):
        for b, n in allowed:
            if b == book_id and (name == n or (n.endswith('*') and name.startswith(n[:-1]))):
                return True
        return False

    return sorted(x for x in before - after if not is_allowed(x[0], x[1]))


def digest(cache):
    """The state of the library as far as a book is concerned, in a form that can be compared"""
    ignored = {'last_modified', 'pages', 'ondevice'}
    ans = {}
    for book_id in sorted(cache.all_book_ids()):
        ans[book_id] = tuple((f, repr(cache.field_for(f, book_id))) for f in sorted(cache.fields) if f not in ignored)
    return ans, snapshot(cache)


# }}}


# Fault injection {{{
@dataclass(frozen=True)
class Fault:
    kind: str  # 'file' or 'sql'
    target: str  # Hook name for files, normalized text of the SQL statement
    nth: int = 1  # Which call to the hook/statement fails
    mode: str = 'before'  # For files 'before' or 'after' the work is done

    @property
    def id(self):
        return f'{self.kind}:{self.target}@{self.nth}' + ('/after' if self.mode == 'after' else '')


def normalize_sql(sql):
    return ' '.join(sql.split()).lower()[:90]


def file_hook_targets(backend):
    """
    The (name, object, attribute, modes) of the functions that perform the file system operations of
    the backend. Only those that do the actual work are listed, not those that only call them. modes
    are the ways in which they are made to fail: before they do their work, which leaves everything
    as it was and after, which leaves the file system changed but the caller unaware of it. The
    second is only used for the helpers whose work cannot be undone by the next step.
    """
    import calibre.db.backend as backend_module
    import calibre.db.notes.connect as notes_module
    import calibre.utils.copy_files as copy_files_module

    both, before = ('before', 'after'), ('before',)
    return [
        ('backend.add_format', backend, 'add_format', both),
        ('backend.set_cover', backend, 'set_cover', both),
        ('backend.add_extra_file', backend, 'add_extra_file', both),
        ('backend.rename_extra_file', backend, 'rename_extra_file', both),
        ('backend.remove_extra_files', backend, 'remove_extra_files', both),
        ('backend.rename_format_file', backend, 'rename_format_file', before),
        ('backend.write_backup', backend, 'write_backup', both),
        ('backend.get_metadata_for_trash_book', backend, 'get_metadata_for_trash_book', before),
        ('copy_tree', backend_module, 'copy_tree', both),
        ('copy_files', backend_module, 'copy_files', both),
        ('rename_files', backend_module, 'rename_files', both),
        ('atomic_rename', backend_module, 'atomic_rename', both),
        ('atomic_write', backend_module, 'atomic_write', both),
        ('remove_dir_if_empty', backend_module, 'remove_dir_if_empty', before),
        ('rmtree_with_retry', backend_module, 'rmtree_with_retry', before),
        # The two phases of copying a folder or files: copy then delete the source
        ('copier.copy_all', copy_files_module.UnixFileCopier, 'copy_all', before),
        ('copier.delete_sources', copy_files_module.UnixFileCopier, 'delete_all_source_files', before),
        ('notes.set_backup_for', backend.notes, 'set_backup_for', both),
        ('notes.retire_entry', backend.notes, 'retire_entry', both),
        ('notes.remove_with_retry', notes_module, 'remove_with_retry', before),
    ]


class Hooks:
    """Record the writes to the database and file system operations done by the thread that
    creates it while installed and, if fault is not None, fail at that fault"""

    def __init__(self, cache, fault=None):
        self.cache = cache
        self.fault = fault
        self.thread = threading.current_thread()
        self.counts = Counter()
        self.trace = []  # (kind, target) in order of occurrence
        self.patched = []
        self.fired = False
        self.success_state = None

    def _hit(self, kind, target):
        self.counts[(kind, target)] += 1
        self.trace.append((kind, target))
        return self.counts[(kind, target)]

    def _should_fail(self, kind, target, mode='before'):
        n = self._hit(kind, target) if mode == 'before' else self.counts[(kind, target)]
        f = self.fault
        return f is not None and (f.kind, f.target, f.nth, f.mode) == (kind, target, n, mode)

    def _file_wrapper(self, name, orig):
        @wraps(orig)
        def wrapper(*args, **kw):
            if threading.current_thread() is not self.thread:
                return orig(*args, **kw)
            if self._should_fail('file', name, 'before'):
                self.fired = True
                raise OSError(errno.EIO, f'{INJECTED}: before {name}')
            ans = orig(*args, **kw)
            if self._should_fail('file', name, 'after'):
                self.fired = True
                raise OSError(errno.EIO, f'{INJECTED}: after {name}')
            return ans

        return wrapper

    def _trace_sql(self, cursor, sql, bindings):
        # Called by SQLite for every statement, for all cursors of the connection, before it is executed
        if WRITE_SQL.match(sql) and threading.current_thread() is self.thread and not IGNORED_SQL.search(sql):
            key = normalize_sql(sql)
            if self._should_fail('sql', key):
                self.fired = True
                raise apsw.SQLError(f'{INJECTED}: {key}')
        return True

    def _patch(self, obj, attr, wrapper_maker, *args):
        orig = getattr(obj, attr)
        setattr(obj, attr, wrapper_maker(*args, orig))
        self.patched.append((obj, attr, orig, attr in getattr(obj, '__dict__', {})))

    def __enter__(self):
        backend = self.cache.backend
        self.modes = {}
        for name, obj, attr, modes in file_hook_targets(backend):
            self.modes[name] = modes
            self._patch(obj, attr, self._file_wrapper, name)
        self.old_trace = backend.conn.exec_trace
        backend.conn.exec_trace = self._trace_sql
        return self

    def __exit__(self, *a):
        self.cache.backend.conn.exec_trace = self.old_trace
        for obj, attr, orig, was_in_dict in reversed(self.patched):
            if was_in_dict:
                setattr(obj, attr, orig)
            else:
                delattr(obj, attr)
        self.patched = []

    def candidate_faults(self, kinds=('file', 'sql'), sql_last=False):
        """The faults to inject in an operation that has this trace: at the first and the last
        occurrence of every distinct file operation, and the first of every SQL statement (and the
        last as well if sql_last is True, for operations that execute a statement for every book,
        or for every format of a book, without a transaction around them)"""
        ans = []
        seen = set()
        for (kind, target), count in self.counts.items():
            if kind not in kinds:
                continue
            if kind == 'sql':
                # Statements that differ only in numbers, such as the tables of custom columns, are one
                shape = re.sub(r'\d+', 'N', target)
                if shape in seen:
                    continue
                seen.add(shape)
                ans.extend(Fault(kind, target, nth) for nth in sorted({1, count} if sql_last else {1}))
                continue
            for nth in sorted({1, count}):
                # A failure after the work was done is only interesting for the first call, later calls
                # are for later books, and what happens to the first one is the same
                ans.extend(Fault(kind, target, nth, mode) for mode in self.modes[target] if mode == 'before' or nth == 1)
        return ans


# }}}


# Cases {{{
@dataclass
class Case:
    """
    One operation to test.

    :param id: Unique name, used in test names and in KNOWN_INCONSISTENT
    :param method: The name of the Cache method that is tested
    :param touches: What the operation modifies: sql, files or sql+files
    :param op: Callable taking the cache and performing the operation
    :param verify: Callable taking the cache and asserting that the operation had its effect, using unittest assertions.
        It is also called after a retry
    :param setup: Callable taking the cache, run after the library is created and before anything is recorded
    :param lost: Collection of (book_id, name), see :func:`lost_files`, of the files the operation destroys when it works. They
        are also not counted as lost when it fails, as it could have done a part of its work
    :param state: Optional callable taking the cache and returning a comparable value, the part of the state that the operation
        changes atomically. After a failure it must be equal to what it was before the operation or to what it is after the
        operation worked, otherwise the problem not-atomic is reported
    :param faults: The kinds of faults to inject, file and/or sql. Use only file for operations whose SQL steps are those
        of other methods that have their own case
    :param sql_last: Also inject a failure of the last execution of every SQL statement, see :meth:`Hooks.candidate_faults`
    :param skip: fnmatch patterns of the ids of faults that are not injected, because they are the same as others
    :param ignore: fnmatch patterns of the kinds of problems that are not reported, for operations that only take effect when
        the library is opened again, such as changes of the custom columns, so the live cache is expected to differ from a fresh one
    :param self_failing: The operation fails by itself, no faults are injected and it must raise
    """

    id: str
    method: str
    touches: str
    op: Callable[[Any], Any]
    verify: Callable[[Any], None] | None = None
    setup: Callable[[Any], None] | None = None
    lost: Any = frozenset()
    state: Callable[[Any], Any] | None = None
    self_failing: bool = False
    faults: tuple = ('file', 'sql')
    skip: tuple = ()
    sql_last: bool = False
    ignore: tuple = ()
    note: str = ''


@dataclass
class Outcome:
    case: Case
    fault: Fault | None
    error: str = ''  # Repr of the exception raised by the operation
    problems: list = field(default_factory=list)  # Inconsistencies, (kind, message)
    lost: list = field(default_factory=list)  # Files lost
    changed: bool = False  # State is different from before the operation
    retry: str = ''  # Result of retrying: ok, error: ... or inconsistent
    retry_problems: list = field(default_factory=list)
    verify_error: str = ''  # For a normal run, why the operation did not have its expected effect
    not_atomic: bool = False
    state_after: Any = None

    @property
    def kinds(self):
        ans = {kind for kind, _msg in self.problems}
        if self.lost:
            ans.add('lost-file')
        if self.not_atomic:
            ans.add('not-atomic')
        return frozenset(ans)

    @property
    def label(self):
        if self.kinds:
            return 'INCONSISTENT: ' + ', '.join(sorted(self.kinds))
        ans = 'changed' if self.changed else 'rolled back'
        if not self.error:
            ans += ' (no error)'
        return ans

    @property
    def description(self):
        parts = [f'{kind}: {msg}' for kind, msg in self.problems[:6]]
        parts += [f'lost file: book {b} {n}' for b, n, _h in self.lost[:6]]
        parts += [f'after retry {kind}: {msg}' for kind, msg in self.retry_problems[:3]]
        return '\n'.join(parts)


def check(test, cache, before, case):
    problems = []
    try:
        problems = test.find_library_inconsistencies(cache)
    except Exception as err:
        problems = [('check-error', f'The consistency check failed: {err!r}')]
    problems = [(kind, msg) for kind, msg in problems if not any(fnmatch.fnmatchcase(kind, p) for p in case.ignore)]
    try:
        lost = lost_files(before, snapshot(cache), case.lost)
    except Exception as err:
        problems.append(('check-error', f'Collecting the files failed: {err!r}'))
        lost = []
    return problems, lost


def open_library(test, case):
    library_path = test.mkdtemp()
    shutil.copytree(template_library(test), library_path, dirs_exist_ok=True)
    cache = test.init_cache(library_path)
    # Only the consistency of what is written matters here, not that it survives a power failure, and fsync is slow
    cache.backend.execute('PRAGMA synchronous=OFF')
    stop_page_counting(cache)
    if case.setup is not None:
        case.setup(cache)
    return cache


def run_case(test, case, fault=None, success_state=None):
    """Run the case once, either normally (fault is None) or with the specified fault. Returns an Outcome
    and, for a normal run, the Hooks that recorded the trace."""
    cache = open_library(test, case)
    before = snapshot(cache)
    # Reading all the fields also fills the cache of the composite columns, so that stale values can be detected
    state = digest(cache)
    atomic_before = case.state(cache) if case.state is not None else None
    error = ''
    with Hooks(cache, fault) as hooks:
        try:
            case.op(cache)
        except Exception as err:
            error = f'{type(err).__name__}: {err}'
            if fault is not None and not hooks.fired and not case.self_failing:
                raise AssertionError(f'The fault {fault.id} was not reached') from err
    if fault is not None and not case.self_failing and not hooks.fired:
        raise AssertionError(f'The fault {fault.id} was not reached')
    outcome = Outcome(case, fault, error=error)
    outcome.problems, outcome.lost = check(test, cache, before, case)
    if fault is None and not error and case.verify is not None:
        try:
            case.verify(cache)
        except AssertionError as err:
            outcome.verify_error = str(err) or repr(err)
    if COLLECT_TABLE:
        try:
            outcome.changed = digest(cache) != state
        except Exception:
            outcome.changed = True
    if case.state is not None:
        outcome.state_after = case.state(cache)
        if fault is not None and error:
            outcome.not_atomic = outcome.state_after not in (atomic_before, success_state)
    if COLLECT_TABLE and error and not outcome.problems and not outcome.lost:
        # The library is fine. Now that the problem is gone, the operation must work if tried again
        try:
            case.op(cache)
            if case.verify is not None:
                case.verify(cache)
        except Exception as err:
            outcome.retry = f'error: {type(err).__name__}: {err}'
        else:
            outcome.retry = 'ok'
        outcome.retry_problems, retry_lost = check(test, cache, before, case)
        if outcome.retry_problems or retry_lost:
            outcome.retry = 'inconsistent'
    # Do not keep the hundreds of caches that a case opens until the end of the test
    test.objects_to_close.remove(cache)
    cache.close()
    return outcome, hooks


class FaultInjectionTest(BaseTest):
    """
    Base class of test classes that run lists of cases. Sub classes set CASES and
    KNOWN_INCONSISTENT, a dict mapping (case id, fault id) to (kinds, description) where kinds
    is the set of the kinds of problems (see :meth:`BaseTest.find_library_inconsistencies`,
    plus lost-file and not-atomic) that are left in the library and description
    explains it in a line. The test methods are generated from CASES.
    """

    CASES: tuple[Case, ...] = ()
    KNOWN_INCONSISTENT: dict[tuple[str, str], tuple[frozenset, str]] = {}
    # Every case closes the caches that it opens. Other platforms need this for deleting files that are in use
    collect_garbage_after_test = iswindows

    def setUp(self):
        from calibre.utils.recycle_bin import nuke_recycle

        nuke_recycle()
        self.paths_to_remove = []
        self.objects_to_close = []

    # Results of all the cases that were run in this process, for the table in the documentation
    results: list[Outcome] = []

    def run_one_case(self, case):
        outcome, hooks = run_case(self, case)
        hooks.success_state = outcome.state_after
        FaultInjectionTest.results.append(outcome)
        self.assertFalse(outcome.error, f'{case.id}: The operation failed: {outcome.error}')
        self.assertEqual(outcome.verify_error, '', f'{case.id}: The operation did not have its effect')
        known = self.known_for(case.id, 'normal')
        if known is None:
            self.assertEqual(outcome.kinds, frozenset(), f'{case.id}: The library is not consistent after the operation\n{outcome.description}')
        else:
            kinds, description = known
            self.assertEqual(
                outcome.kinds, frozenset(kinds), f'{case.id}: The problems after the operation are not the documented ones ({description})\n{outcome.description}'
            )
        return hooks

    def known_for(self, case_id, fault_id):
        """The (kinds, description) from KNOWN_INCONSISTENT for the fault, or None. The fault id in
        KNOWN_INCONSISTENT can be a pattern that uses * to match the fault ids"""
        ans = [v for (c, pattern), v in self.KNOWN_INCONSISTENT.items() if c == case_id and fnmatch.fnmatchcase(fault_id, pattern)]
        self.assertLessEqual(len(ans), 1, f'More than one KNOWN_INCONSISTENT entry for {case_id} {fault_id}')
        return ans[0] if ans else None

    def run_failures(self, case, hooks):
        faults = [f for f in hooks.candidate_faults(case.faults, case.sql_last) if not any(fnmatch.fnmatchcase(f.id, p) for p in case.skip)]
        # Entries for faults that are not injected any more must be removed
        stale = {
            pattern
            for (c, pattern) in self.KNOWN_INCONSISTENT
            if c == case.id and pattern != 'normal' and not any(fnmatch.fnmatchcase(f.id, pattern) for f in faults)
        }
        self.assertFalse(stale, f'KNOWN_INCONSISTENT has entries for faults that are not injected: {sorted(stale)}')
        for fault in faults:
            known = self.known_for(case.id, fault.id)
            with self.subTest(case=case.id, fault=fault.id):
                outcome, _ = run_case(self, case, fault, hooks.success_state)
                FaultInjectionTest.results.append(outcome)
                if known is None:
                    self.assertEqual(
                        outcome.kinds, frozenset(), f'{case.id}: The library is not consistent after the failure {fault.id}\n{outcome.description}'
                    )
                else:
                    kinds, description = known
                    self.assertEqual(
                        outcome.kinds,
                        frozenset(kinds),
                        f'{case.id}: The problems after the failure {fault.id} are not the documented ones ({description})\n{outcome.description}',
                    )

    def run_self_failing_case(self, case):
        with self.subTest(case=case.id):
            outcome, _ = run_case(self, case)
            FaultInjectionTest.results.append(outcome)
            self.assertTrue(outcome.error, f'{case.id}: The operation was expected to fail')
            known = self.known_for(case.id, 'self')
            if known is None:
                self.assertEqual(outcome.kinds, frozenset(), f'{case.id}: The library is not consistent after the failure\n{outcome.description}')
            else:
                kinds, description = known
                self.assertEqual(
                    outcome.kinds, frozenset(kinds), f'{case.id}: The problems after the failure are not the documented ones ({description})\n{outcome.description}'
                )


def make_test(case):
    def test(self):
        if case.self_failing:
            self.run_self_failing_case(case)
            return
        hooks = self.run_one_case(case)
        self.run_failures(case, hooks)

    test.__doc__ = f'{case.method} ({case.touches}): {case.id}'
    return test


def add_case_tests(cls):
    "Class decorator generating a test method for every case in cls.CASES"
    for case in cls.CASES:
        setattr(cls, f'test_{case.id}', make_test(case))
    return cls


def write_table(results, path):
    """Write a markdown table of the specified outcomes"""
    by_case = defaultdict(list)
    order = []
    for o in results:
        if o.case.id not in by_case:
            order.append(o.case)
        by_case[o.case.id].append(o)
    lines = ['| case | method | touches | normal | fault | outcome | detail |', '|---|---|---|---|---|---|---|']
    for case in order:
        for o in by_case[case.id]:
            if o.fault is None and not case.self_failing:
                detail = o.description.replace('\n', ' / ').replace('|', '/')[:600]
                lines.append(f'| {case.id} | {case.method} | {case.touches} | {"ok" if not o.kinds else o.label} | | | {detail} |')
            else:
                detail = o.description.replace('\n', ' / ').replace('|', '/')[:300]
                lines.append(f'| {case.id} | {case.method} | {case.touches} | | {o.fault.id if o.fault else "self"} | {o.label}; retry={o.retry or "-"} | {detail} |')
    with open(path, 'a') as f:
        f.write('\n'.join(lines) + '\n')


# }}}



# Describing what is left inconsistent {{{
KIND_TEXT = {
    'open': 'the library cannot be opened',
    'ids': 'the books the cache knows are not those in the database',
    'formats': 'the formats of a book differ between memory and the database',
    'composite-cache': 'cached values of composite columns are stale',
    'format-file-missing': 'a format listed for a book is missing from its folder',
    'cover-file-missing': 'a book marked as having a cover has no cover file',
    'stray-cover': 'a book marked as having no cover has a cover file',
    'folder-missing': 'the folder the database has for a book does not exist',
    'orphan-folder': 'a folder exists that belongs to no book',
    'format-size': 'the recorded size of a format is not the size of its file',
    'prefs': 'a preference differs between memory and the database',
    'lost-file': 'the contents of a file were lost',
    'not-atomic': 'only a part of the changes was made',
    'note-orphan': 'a note belongs to an item that is not in memory',
    'note-resource-missing': 'a note refers to a resource file that was deleted',
    'note-backup-missing': 'the backup file of a note is missing',
    'notes-differ': 'the items that have notes differ between memory and the database',
    'check-error': 'the check itself failed',
}


def describe(kinds):
    "A line describing what is wrong, given the kinds of problems"
    fields = sorted(k.partition(':')[2] for k in kinds if k.startswith('field:'))
    items = sorted(k.partition(':')[2] for k in kinds if k.startswith('items:'))
    parts = []
    if fields:
        parts.append(f'the value of {", ".join(fields)} differs between memory and the database')
    if items:
        parts.append(f'the items of {", ".join(items)} differ between memory and the database')
    parts += [KIND_TEXT[k] for k in sorted(kinds) if not k.startswith(('field:', 'items:'))]
    return '; '.join(parts)


def known_table(*entries):
    """
    Build KNOWN_INCONSISTENT from entries of the form (case id, kinds, fault ids or patterns[, description]).
    A fault pattern can use * to match any characters. kinds are the kinds of problems that are found after
    the faults, see :meth:`BaseTest.find_library_inconsistencies`, plus lost-file and not-atomic. The
    description is generated from the kinds if it is not specified.
    """
    ans = {}
    for entry in entries:
        case_id, kinds, patterns = entry[:3]
        description = entry[3] if len(entry) > 3 else describe(kinds)
        for pattern in patterns:
            assert (case_id, pattern) not in ans, (case_id, pattern)
            ans[(case_id, pattern)] = (frozenset(kinds), description)
    return ans


# }}}


# What the write methods of the Cache modify {{{
SQL, FILES, SQL_FILES, MEMORY = 'sql', 'files', 'sql+files', 'memory'
WRITE_METHODS = {
    # Only the in-memory state of the Cache, or its read-only use of the database
    'ensure_has_search_category': MEMORY,
    'initialize_dynamic': MEMORY,
    'initialize_template_cache': MEMORY,
    'set_user_template_functions': MEMORY,
    'clear_composite_caches': MEMORY,
    'clear_search_caches': MEMORY,
    'clear_extra_files_cache': MEMORY,
    'clear_caches': MEMORY,
    'clear_link_map_cache': MEMORY,
    'reload_from_db': MEMORY,
    'fts_start_measuring_rate': MEMORY,
    'add_listener': MEMORY,
    'remove_listener': MEMORY,
    'add_cover_cache': MEMORY,
    'remove_cover_cache': MEMORY,
    'refresh_format_cache': MEMORY,
    'refresh_ondevice': MEMORY,
    'change_search_locations': MEMORY,
    'refresh_search_locations': MEMORY,
    'fts_search': MEMORY,  # Read only, takes the write lock because of limitations of SQLite
    'search_notes': MEMORY,  # Same
    # Only SQLite (the main database, or the separate full text search database)
    'set_pref': SQL,
    'saved_search_set_all': SQL,
    'saved_search_add': SQL,
    'saved_search_delete': SQL,
    'saved_search_rename': SQL,
    'refresh_format_sizes': SQL,
    'update_last_modified': SQL,
    'mark_as_dirty': SQL,
    'commit_dirty_cache': SQL,
    'check_dirtied_annotations': SQL,
    'clear_dirtied': SQL,
    'mark_for_pages_recount': SQL,
    'queue_pages_scan': SQL,
    'set_pages': SQL,
    'add_custom_book_data': SQL,
    'delete_custom_book_data': SQL,
    'set_conversion_options': SQL,
    'delete_conversion_options': SQL,
    'set_sort_for_authors': SQL,
    'set_link_for_authors': SQL,
    'set_link_map': SQL,
    'create_custom_column': SQL,
    'delete_custom_column': SQL,
    'set_custom_column_metadata': SQL,
    'set_last_read_position': SQL,
    'delete_annotations': SQL,
    'update_annotations': SQL,
    'restore_annotations': SQL,
    'set_annotations_for_book': SQL,
    'merge_annotations_for_book': SQL,
    'save_annotations_list': SQL,
    'update_book_storage_for_book': SQL,
    'save_book_storage': SQL,
    'reindex_annotations': SQL,
    'enable_fts': SQL,
    'fts_unindex': SQL,
    'queue_next_fts_job': SQL,
    'commit_fts_result': SQL,
    'reindex_fts_book': SQL,
    'set_fts_num_of_workers': SQL,
    'set_fts_speed': SQL,
    # SQLite and the file system
    'set_field': SQL_FILES,  # Only the title and authors fields rename the folder of the book
    'set_cover': SQL_FILES,
    'set_metadata': SQL_FILES,
    'add_format': SQL_FILES,
    'remove_formats': SQL_FILES,
    'create_book_entry': SQL_FILES,
    'add_books': SQL_FILES,
    'remove_books': SQL_FILES,
    'rename_items': SQL_FILES,  # Renaming an author renames the folder of its books, notes are also files
    'remove_items': SQL_FILES,  # Notes are files
    'update_path': SQL_FILES,
    'save_original_format': SQL_FILES,
    'restore_original_format': SQL_FILES,
    'move_format_from_trash': SQL_FILES,
    'move_book_from_trash': SQL_FILES,
    'restore_book': SQL_FILES,
    'merge_book_metadata': SQL_FILES,
    'set_notes_for': SQL_FILES,
    'add_notes_resource': SQL_FILES,
    'unretire_note_for': SQL_FILES,
    'import_note': SQL_FILES,
    'move_library_to': SQL_FILES,
    'dump_and_restore': SQL_FILES,
    'vacuum': SQL_FILES,
    # Only the file system
    'delete_trash_entry': FILES,
    'expire_old_trash': FILES,
    'clear_trash_bin': FILES,
    'write_backup': FILES,
    'dump_metadata': FILES,
    'compress_covers': FILES,
    'embed_metadata': FILES,
    'add_extra_files': FILES,
    'rename_extra_files': FILES,
    'merge_extra_files': FILES,
    'remove_extra_files': FILES,
    'export_library': FILES,  # Writes outside the library
}
# Methods that are not decorated with one of the write APIs, but write
WRITE_METHODS_WITHOUT_LOCK_DECORATOR = ('add_format', 'add_books', 'save_original_format')


def write_api_methods():
    "The names of all the Cache methods that take the write lock, plus those that write without being decorated"
    from calibre.db.cache import cache_api

    return {name for name, locked in cache_api.items() if locked} | set(WRITE_METHODS_WITHOUT_LOCK_DECORATOR)


# }}}
