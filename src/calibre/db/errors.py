#!/usr/bin/env python
# License: GPLv3 Copyright: 2011, Kovid Goyal <kovid@kovidgoyal.net>


class NoSuchFormat(ValueError):
    pass


class NoTracebackException(Exception):
    suppress_traceback: bool = True


class NoSuchBook(KeyError):
    def __init__(self, book_id):
        KeyError.__init__(self, f'No book with id: {book_id} in database')
        self.book_id = book_id


class BooksNotRemoved(Exception):
    '''
    Raised by :meth:`Cache.remove_books` when some, but possibly not all, of
    the books could not be removed. The books that could not be removed are
    left completely untouched in the library. The books that could be removed
    are really removed, and listed in ``removed``.

    :ivar failures: Mapping of book id to a string containing the traceback of the error
    :ivar removed: Tuple of ids of books that were successfully removed
    '''

    def __init__(self, failures, removed=()):
        self.failures = dict(failures)
        self.removed = tuple(removed)
        ids = ', '.join(map(str, sorted(self.failures)))
        first = next(iter(self.failures.values()), '').strip().splitlines()
        reason = first[-1] if first else ''
        Exception.__init__(self, f'Failed to remove the books with ids: {ids}. First error: {reason}')
