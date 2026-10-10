#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>
"""
The change record of Cache._change(), the write path's unit of work.

Design
------
A write method runs inside ``with self._change() as change:``. The context gives it

* one SQLite transaction (a savepoint when it is nested in another transaction or
  another _change()), committed when the block exits normally;
* a ChangeRecord that the write code fills in: which fields were changed, for which
  books and which items (``change.touch()``), the tables whose in-memory state the
  write modifies (``change.modifies()``), and callbacks (functools.partial) to run once the change is
  committed (``change.after_commit()``);
* deferral of irreversible file operations of the notes (see
  Notes.deferred_file_operations()), run only after the commit.

If the block raises, the SQL is rolled back and the in-memory tables listed with
``modifies()`` are re-read from the database, so memory never keeps a change the
database does not have, and the caches derived from the changed books are cleared.
Writers still change the in-memory tables next to their SQL statements (making them
apply memory only after the SQL succeeded is the job of the per-method migrations),
the re-read is what makes that safe.

Only when the outermost context commits, the record is applied: the cache of computed
categories is told of the touched fields, the ``after_commit`` callbacks run in the order
they were added, then the functions in ``Cache.change_listeners`` are called with the
record. Those listeners are how a derived structure (the cache restructuring of D3)
subscribes to every change without being called by each write method.

A file operation that must not happen unless the change commits is staged first (for
example a new format is copied to a temporary file) and finished from ``after_commit()``,
with ``on_rollback()`` removing what was staged. The first callback that fails does not
prevent the others from running, its exception is raised after all of them ran.

Migrated so far: set_field, add_format and remove_formats. add_format stages the new contents in a
temporary file (removed by on_rollback()) and replaces the format file in an after_commit() callback;
remove_formats moves the files to the trash from after_commit() and runs the post delete plugins and
the formats_removed event there too. The formats table and the size table are re-read together on
rollback (the sizes are derived from the scan of the data table made when the formats are read).

Residuals: a failure of an after_commit() file operation (disk full, permissions) leaves the database
changed and the file as it was, it is raised to the caller; a ``_change()`` entered while the caller already holds a transaction of its
own (set_metadata still does) is applied when its savepoint is released, which is before
the outer transaction commits. File operations other than the notes' (for example moving
the folder of a book) are not deferred unless the write method adds them with
``after_commit()``.
"""


class ChangeRecord:
    def __init__(self):
        self.fields = {}  # field name -> [set of book ids, set of item ids or None for "any/unknown"]
        self.tables = {}  # table name -> table, to re-read from the db if the change fails
        self.callbacks = []
        self.rollback_callbacks = []

    def touch(self, field_name, book_ids=(), item_ids=None, report_books=True):
        """
        Record that the field changed for these books and (when known) these items of the field. item_ids None means
        any item. If report_books is False the cache of categories is told only that the field changed, not for which books.
        """
        entry = self.fields.get(field_name)
        if entry is None:
            self.fields[field_name] = [set(book_ids), None if item_ids is None else set(item_ids), report_books]
        else:
            entry[0].update(book_ids)
            if item_ids is None or entry[1] is None:
                entry[1] = None
            else:
                entry[1].update(item_ids)
            entry[2] = entry[2] and report_books

    def modifies(self, *tables):
        "Register the in-memory tables the write changes, they are re-read from the db if the change fails"
        for t in tables:
            self.tables[t.name] = t

    def after_commit(self, func):
        "Run func() after the change is committed"
        self.callbacks.append(func)

    def on_rollback(self, func):
        "Run func() if the change fails, to undo what cannot be undone by the database, such as removing a staged file"
        self.rollback_callbacks.append(func)

    @property
    def book_ids(self):
        ans = set()
        for books, items, report_books in self.fields.values():
            ans |= books
        return ans

    def merge(self, other):
        for name, (books, items, report_books) in other.fields.items():
            self.touch(name, books, items, report_books)
        self.tables.update(other.tables)
        self.callbacks.extend(other.callbacks)
        self.rollback_callbacks.extend(other.rollback_callbacks)
