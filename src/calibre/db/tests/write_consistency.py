#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

"""
Do the write methods of the Cache leave the library consistent, when they work and
when they fail part way? See fault_injection.py for how the cases are run.

To add a method to the table add a Case to CASES, in one line if the operation is a call
of the method. The run lists the faults that make it inconsistent, put them in KNOWN_INCONSISTENT
with a description of what is left wrong, so that the test documents the weakness and fails
when it is fixed, to remind you to remove the entry. Run with the environment variable
CALIBRE_WRITE_CONSISTENCY_TABLE=/path/to/file.md to get a table of all the results.
"""

import os
import unittest
from io import BytesIO

from calibre.db.tests.base import IMG
from calibre.db.tests.fault_injection import A, B, C, D, TABLE_PATH, Case, FaultInjectionTest, add_case_tests, known_table, write_table

_tc = unittest.TestCase()
eq, true, false = _tc.assertEqual, _tc.assertTrue, _tc.assertFalse
_tc.maxDiff = None


def metadata(*args, **kw):
    from calibre.ebooks.metadata.book.base import Metadata

    return Metadata(*args, **kw)


def item_id(cache, field, name):
    return cache.get_item_id(field, name)


class FailingStream:
    "A file that fails to be read after delivering the first bytes"

    def __init__(self, data=b'partial data'):
        self.data = data

    def read(self, size=-1):
        if self.data:
            ans, self.data = self.data, b''
            return ans
        raise OSError('injected failure: cannot read the source file')


def has_format(cache, book_id, fmt, data=None):
    true(cache.has_format(book_id, fmt), f'{fmt} missing from book {book_id}')
    if data is not None:
        eq(cache.format(book_id, fmt), data)


def folder_name(cache, book_id):
    return cache.field_for('path', book_id)


def extra_files(cache, book_id):
    return {x.relpath for x in cache.list_extra_files(book_id)}


def notes_state(cache):
    ans = {}
    for field, ids in cache.get_all_items_that_have_notes().items():
        for item in ids:
            ans[(field, item)] = cache.notes_for(field, item)
    return ans


def meta_for_rename():
    mi = metadata('Meta Title', ['Meta Author'])
    mi.tags = ['mt1']
    mi.publisher = 'Meta Publisher'
    mi.cover_data = ('jpg', IMG)
    return mi


def meta_for_fields():
    mi = metadata('Title Two', ['Author Two', 'Author One'])
    mi.tags = ['mt1', 'mt2']
    mi.publisher = 'Meta Publisher'
    mi.series, mi.series_index = 'Meta Series', 3
    mi.comments = 'Some comments'
    mi.rating = 6
    return mi


def new_book():
    mi = metadata('Brand New Book', ['Brand New Author'])
    mi.tags = ['nt1']
    return mi


def sql_field(name, value, expected=None):
    "A case that sets a field that is only stored in the database"
    expected = tuple(value) if isinstance(value, list) else value if expected is None else expected
    # The tag with a note is not used any more when the tags are replaced
    lost = {(0, 'notes/resources/*')} if name == 'tags' else ()
    return Case(
        'set_field_' + name.replace('#', 'custom_'),
        'set_field',
        'sql',
        lambda c: c.set_field(name, {A: value, B: value}),
        lambda c: eq((c.field_for(name, A), c.field_for(name, B)), (expected, expected)),
        lost=lost,
    )


def make_annotations():
    return [({'type': 'bookmark', 'title': 'a bookmark', 'pos': 'epubcfi(/2)', 'timestamp': '2026-01-01T00:00:00+00:00'}, 1767225600.0)]


def old_annotations(cache):
    cache.set_annotations_for_book(A, 'FMT1', make_annotations())


def annotation_titles(cache):
    return sorted(x['annotation'].get('title', '') for x in cache.all_annotations_for_book(A))


CASES = (
    # Formats {{{
    Case(
        'add_format_new',
        'add_format',
        'sql+files',
        lambda c: c.add_format(A, 'FMT3', BytesIO(b'new fmt3')),
        lambda c: has_format(c, A, 'FMT3', b'new fmt3'),
    ),
    Case(
        'add_format_replace',
        'add_format',
        'sql+files',
        lambda c: c.add_format(A, 'FMT1', BytesIO(b'replacement fmt1')),
        lambda c: has_format(c, A, 'FMT1', b'replacement fmt1'),
        lost={(A, 'FMT1')},
    ),
    Case(
        'add_format_replace_unreadable',
        'add_format',
        'sql+files',
        lambda c: c.add_format(A, 'FMT1', FailingStream(), run_hooks=False),
        self_failing=True,
        note='The source of the new contents fails to be read after delivering the first bytes',
    ),
    Case(
        'remove_formats',
        'remove_formats',
        'sql+files',
        lambda c: c.remove_formats({A: ['FMT1'], B: ['FMT1']}),
        lambda c: eq((c.formats(A), c.formats(B)), (('FMT2',), ())),
    ),
    Case(
        'remove_formats_db_only',
        'remove_formats',
        'sql',
        lambda c: c.remove_formats({A: ['FMT2']}, db_only=True),
        lambda c: eq(c.formats(A), ('FMT1',)),
    ),
    Case(
        'restore_original_format',
        'restore_original_format',
        'sql+files',
        lambda c: c.restore_original_format(A, 'ORIGINAL_FMT1'),
        lambda c: has_format(c, A, 'FMT1', b'book1fmt1') and false(c.has_format(A, 'ORIGINAL_FMT1')),
        setup=lambda c: (c.save_original_format(A, 'FMT1'), c.add_format(A, 'FMT1', BytesIO(b'changed fmt1'))),
        lost={(A, 'FMT1'), (A, 'ORIGINAL_FMT1')},
    ),
    # }}}
    # Covers {{{
    Case('set_cover_new', 'set_cover', 'sql+files', lambda c: c.set_cover({C: IMG}), lambda c: true(c.field_for('cover', C))),
    Case('set_cover_remove', 'set_cover', 'sql+files', lambda c: c.set_cover({A: None}), lambda c: false(c.field_for('cover', A)), lost={(A, 'COVER')}),
    Case(
        'set_cover_two_books',
        'set_cover',
        'sql+files',
        lambda c: c.set_cover({C: IMG, B: IMG}),
        lambda c: true(c.field_for('cover', C) and c.field_for('cover', B)),
        lost={(B, 'COVER')},
    ),
    # }}}
    # Renaming the folder of the book {{{
    Case(
        'set_authors',
        'set_field',
        'sql+files',
        lambda c: c.set_field('authors', {A: ['Brand New Author']}),
        lambda c: true(folder_name(c, A).startswith('Brand New Author/') and c.has_format(A, 'FMT2')),
    ),
    Case(
        'set_title_two_books',
        'set_field',
        'sql+files',
        lambda c: c.set_field('title', {A: 'Title A', B: 'Title B'}),
        lambda c: true('Title B' in folder_name(c, B) and 'Title A' in folder_name(c, A)),
        sql_last=True,
    ),
    Case(
        'update_path',
        'update_path',
        'sql+files',
        lambda c: c.update_path({A}),
        lambda c: true('Changed Title' in folder_name(c, A)),
        setup=lambda c: c.set_field('title', {A: 'Changed Title'}, do_path_update=False),
    ),
    Case(
        'set_metadata_rename',
        'set_metadata',
        'sql+files',
        lambda c: c.set_metadata(A, meta_for_rename()),
        lambda c: true('Meta Title' in folder_name(c, A) and c.field_for('tags', A) == ('mt1',)),
        lost={(A, 'COVER'), (0, 'notes/resources/*')},
        faults=('file',),
        note='The SQL steps are those of set_field and set_metadata_fields',
    ),
    Case(
        'set_metadata_fields',
        'set_metadata',
        'sql',
        lambda c: c.set_metadata(A, meta_for_fields()),
        lambda c: eq((c.field_for('tags', A), c.field_for('series', A), c.field_for('publisher', A)), (('mt1', 'mt2'), 'Meta Series', 'Meta Publisher')),
        lost={(0, 'notes/resources/*')},
        note='The tag with a note is not used any more, so its note is retired',
    ),
    # }}}
    # Books {{{
    Case(
        'create_book_entry',
        'create_book_entry',
        'sql+files',
        lambda c: c.create_book_entry(new_book(), cover=IMG),
        lambda c: true(c.field_for('title', 5) == 'Brand New Book' and c.field_for('cover', 5)),
        skip=(
            'sql:update books set sort*',
            'sql:update books set last_modified*',
            'sql:insert or ignore into metadata_dirtied*',
            'sql:delete from books_authors_link*',
            'sql:insert into books_authors_link*',
            'sql:update books set author_sort*',
            'sql:update books set pubdate*',
            'sql:update books set uuid*',
        ),
        note='Errors setting the metadata are ignored, leaving a book with just a title',
    ),
    Case(
        'add_books',
        'add_books',
        'sql+files',
        lambda c: c.add_books([(new_book(), {'FMT1': BytesIO(b'added fmt1'), 'FMT2': BytesIO(b'added fmt2')})], run_hooks=False),
        lambda c: eq(c.formats(5), ('FMT1', 'FMT2')),
        faults=('file',),
        note='The SQL steps are those of create_book_entry and add_format',
    ),
    Case(
        'remove_books',
        'remove_books',
        'sql+files',
        lambda c: c.remove_books([A, B]),
        lambda c: eq(set(c.all_book_ids()), {C}),
        lost={(0, 'notes/resources/*')},
        skip=(
            'sql:delete from custom_column*',
            'sql:delete from languages*',
            'sql:delete from ratings*',
            'sql:delete from publishers*',
            'sql:delete from series*',
        ),
        note='Deleting the items of the other columns that are not used any more fails like tags and authors',
    ),
    # }}}
    # Trash {{{
    Case(
        'move_book_from_trash',
        'move_book_from_trash',
        'sql+files',
        lambda c: c.move_book_from_trash(D),
        lambda c: true({'FMT1', 'FMT2'} <= set(c.formats(D)) and c.field_for('cover', D) and os.path.exists(os.path.join(c.backend.library_path, folder_name(c, D), 'trashed.txt'))),
        skip=(
            'sql:update books set sort*',
            'sql:update books set last_modified*',
            'sql:insert or ignore into metadata_dirtied*',
            'sql:delete from books_authors_link*',
            'sql:insert into books_authors_link*',
            'sql:update books set author_sort*',
            'sql:update books set pubdate*',
            'sql:update books set uuid*',
        ),
        note='The SQL statements of set_metadata fail the same way, the first is representative',
    ),
    Case(
        'move_format_from_trash',
        'move_format_from_trash',
        'sql+files',
        lambda c: c.move_format_from_trash(B, 'FMT3'),
        lambda c: has_format(c, B, 'FMT3', b'trashed fmt3'),
    ),
    Case('delete_trash_entry', 'delete_trash_entry', 'files', lambda c: c.delete_trash_entry(D, 'b'), lost={(D, '*')}),
    Case(
        'expire_old_trash',
        'expire_old_trash',
        'files',
        lambda c: c.expire_old_trash(),
        lambda c: eq({x.book_id for x in c.list_trash_entries()[0]}, set()),
        setup=lambda c: os.utime(os.path.join(c.backend.trash_dir, 'b', str(D)), (1000, 1000)),
        lost={(D, '*')},
        note='Only entries older than a day expire, the one for book D is made old',
    ),
    Case('clear_trash_bin', 'clear_trash_bin', 'files', lambda c: c.clear_trash_bin(), lost={(D, '*'), (B, 'FMT3')}),
    # }}}
    # Items {{{
    Case(
        'rename_items_authors',
        'rename_items',
        'sql+files',
        lambda c: c.rename_items('authors', {item_id(c, 'authors', 'Author One'): 'Renamed One', item_id(c, 'authors', 'Author Two'): 'Renamed Two'}),
        lambda c: true(
            c.field_for('authors', A) == ('Renamed Two', 'Renamed One')
            and folder_name(c, A).startswith('Renamed Two/')
            and folder_name(c, B).startswith('Renamed One/')
            and c.has_format(A, 'FMT2')
            and c.has_format(B, 'FMT1')
        ),
        sql_last=True,
    ),
    Case(
        'rename_items_tags',
        'rename_items',
        'sql',
        lambda c: c.rename_items('tags', {item_id(c, 'tags', 't1'): 'renamed t1', item_id(c, 'tags', 't3'): 'renamed t3'}),
        lambda c: eq((c.field_for('tags', A), c.field_for('tags', C)), (('renamed t1', 't2'), ('renamed t3',))),
    ),
    Case(
        'rename_items_tags_merge',
        'rename_items',
        'sql',
        lambda c: c.rename_items('tags', {item_id(c, 'tags', 't1'): 't2'}),
        lambda c: eq((c.field_for('tags', A), len(c.notes_for('tags', item_id(c, 'tags', 't2')))), (('t2',), len('<p>note one</p>'))),
        note='Merges a tag that has a note with resources into another',
    ),
    Case(
        'rename_items_series',
        'rename_items',
        'sql',
        lambda c: c.rename_items('series', {item_id(c, 'series', 'Series One'): 'Series Renamed'}),
        lambda c: eq((c.field_for('series', A), c.field_for('series', B)), ('Series Renamed', 'Series Renamed')),
    ),
    Case(
        'remove_items_tags',
        'remove_items',
        'sql',
        lambda c: c.remove_items('tags', [item_id(c, 'tags', 't1'), item_id(c, 'tags', 't3')]),
        lambda c: eq((c.field_for('tags', A), c.field_for('tags', C)), (('t2',), ())),
        lost={(0, 'notes/resources/*')},
        note='Removing a tag with a note retires the note',
    ),
    Case(
        'remove_items_custom',
        'remove_items',
        'sql',
        lambda c: c.remove_items('#tags', [item_id(c, '#tags', 'ct1')]),
        lambda c: eq(c.field_for('#tags', A), ('ct2',)),
    ),
    Case(
        'set_link_map',
        'set_link_map',
        'sql',
        lambda c: c.set_link_map('tags', {'t1': 'https://example.com/t1', 't2': 'https://example.com/t2'}),
        lambda c: eq(c.get_link_map('tags'), {'t1': 'https://example.com/t1', 't2': 'https://example.com/t2'}),
    ),
    Case(
        'set_link_for_authors',
        'set_link_for_authors',
        'sql',
        lambda c: c.set_link_for_authors({item_id(c, 'authors', 'Author One'): 'https://example.com/a1'}),
        lambda c: eq(c.link_for('authors', item_id(c, 'authors', 'Author One')), 'https://example.com/a1'),
    ),
    Case(
        'set_sort_for_authors',
        'set_sort_for_authors',
        'sql',
        lambda c: c.set_sort_for_authors({item_id(c, 'authors', 'Author One'): 'Sorted, One'}),
        lambda c: eq(c.field_for('author_sort', B), 'Sorted, One'),
    ),
    # }}}
    # Notes {{{
    Case(
        'add_notes_resource',
        'add_notes_resource',
        'sql+files',
        lambda c: c.add_notes_resource(b'resource two', 'res2.bin'),
        lambda c: true(c.get_notes_resource(c.add_notes_resource(b'resource two', 'res2.bin'))),
    ),
    Case(
        'set_notes_new',
        'set_notes_for',
        'sql+files',
        lambda c: c.set_notes_for('tags', item_id(c, 'tags', 't2'), '<p>note two</p>', resource_hashes=(c.add_notes_resource(b'resource two', 'res2.bin'),)),
        lambda c: eq(c.notes_for('tags', item_id(c, 'tags', 't2')), '<p>note two</p>'),
    ),
    Case(
        'set_notes_replace_resource',
        'set_notes_for',
        'sql+files',
        lambda c: c.set_notes_for('tags', item_id(c, 'tags', 't1'), '<p>note changed</p>', resource_hashes=(c.add_notes_resource(b'resource three', 'res3.bin'),)),
        lambda c: eq(c.notes_for('tags', item_id(c, 'tags', 't1')), '<p>note changed</p>'),
        lost={(0, 'notes/resources/*')},
        state=notes_state,
    ),
    Case(
        'set_notes_clear',
        'set_notes_for',
        'sql+files',
        lambda c: c.set_notes_for('tags', item_id(c, 'tags', 't1'), ''),
        lambda c: eq(c.notes_for('tags', item_id(c, 'tags', 't1')), ''),
        lost={(0, 'notes/resources/*')},
        state=notes_state,
    ),
    Case(
        'unretire_note_for',
        'unretire_note_for',
        'sql+files',
        lambda c: c.unretire_note_for('tags', item_id(c, 'tags', 't1')),
        lambda c: eq(c.notes_for('tags', item_id(c, 'tags', 't1')), '<p>note one</p>'),
        setup=lambda c: c.set_notes_for('tags', item_id(c, 'tags', 't1'), ''),
        lost={(0, 'notes/retired/*')},
    ),
    Case(
        'import_note',
        'import_note',
        'sql+files',
        lambda c: c.import_note('tags', item_id(c, 'tags', 't2'), '<html><body><p>imported</p></body></html>', path_is_data=True),
        lambda c: true('imported' in c.notes_for('tags', item_id(c, 'tags', 't2'))),
    ),
    # }}}
    # Extra files {{{
    Case(
        'add_extra_files_new',
        'add_extra_files',
        'files',
        lambda c: c.add_extra_files(A, {'new.txt': BytesIO(b'new extra'), 'data/more/y.bin': BytesIO(b'y')}),
        lambda c: eq(extra_files(c, A), {'notes.txt', 'data/x.bin', 'new.txt', 'data/more/y.bin'}),
    ),
    Case(
        'add_extra_files_replace',
        'add_extra_files',
        'files',
        lambda c: c.add_extra_files(A, {'notes.txt': BytesIO(b'replaced notes')}),
        lambda c: eq(extra_files(c, A), {'notes.txt', 'data/x.bin'}),
        lost={(A, 'EXTRA')},
    ),
    Case(
        'add_extra_files_replace_unreadable',
        'add_extra_files',
        'files',
        lambda c: c.add_extra_files(A, {'notes.txt': FailingStream()}),
        self_failing=True,
        note='The source of the new contents fails to be read after delivering the first bytes',
    ),
    Case(
        'rename_extra_files',
        'rename_extra_files',
        'files',
        lambda c: c.rename_extra_files(A, {'notes.txt': 'data/notes-moved.txt', 'data/x.bin': 'x2.bin'}),
        lambda c: eq(extra_files(c, A), {'data/notes-moved.txt', 'x2.bin'}),
    ),
    Case(
        'merge_extra_files',
        'merge_extra_files',
        'files',
        lambda c: c.merge_extra_files(A, [B]),
        lambda c: true('extra2.txt' in extra_files(c, A)),
    ),
    Case(
        'remove_extra_files',
        'remove_extra_files',
        'files',
        lambda c: c.remove_extra_files(A, ['notes.txt', 'data/x.bin'], permanent=True),
        lambda c: eq(extra_files(c, A), set()),
        lost={(A, 'EXTRA')},
    ),
    # }}}
    # Merging {{{
    Case(
        'merge_book_metadata',
        'merge_book_metadata',
        'sql+files',
        lambda c: c.merge_book_metadata(C, [A]),
        lambda c: true(c.field_for('cover', C) and set(c.field_for('tags', C)) == {'t1', 't2', 't3'}),
        faults=('file',),
        note='The SQL steps are those of set_metadata_fields',
    ),
    Case(
        'merge_book_metadata_alternate_cover',
        'merge_book_metadata',
        'sql+files',
        lambda c: c.merge_book_metadata(B, [A], replace_cover=True, save_alternate_cover=True),
        lambda c: true('data/cover-001.jpg' in extra_files(c, B) and c.field_for('cover', B)),
        lost={(B, 'COVER')},
        faults=('file',),
    ),
    # }}}
    # Only the database {{{
    sql_field('tags', ['x1', 'x2']),
    sql_field('series', 'New Series'),
    sql_field('rating', 4),
    sql_field('identifiers', {'isbn': '123456789X', 'doi': 'x/y'}),
    sql_field('#tags', ['cx1', 'cx2']),
    sql_field('#series', 'Custom Series'),
    Case(
        'mark_as_dirty',
        'mark_as_dirty',
        'sql',
        lambda c: c.mark_as_dirty({A, B}),
        lambda c: true({A, B} <= set(c.dirtied_cache)),
        setup=lambda c: [c.clear_dirtied(b, s) for b, s in tuple(c.dirtied_cache.items())],
    ),
    Case('set_pref', 'set_pref', 'sql', lambda c: c.set_pref('write_consistency_pref', {'a': 1}), lambda c: eq(c.pref('write_consistency_pref'), {'a': 1})),
    Case(
        'saved_search_add',
        'saved_search_add',
        'sql',
        lambda c: c.saved_search_add('my search', 'tags:t1'),
        lambda c: eq(c.saved_search_lookup('my search'), 'tags:t1'),
    ),
    Case(
        'set_conversion_options',
        'set_conversion_options',
        'sql',
        lambda c: c.set_conversion_options({A: {'a': 1}, B: {'b': 2}}),
        lambda c: true(c.has_conversion_options([A, B])),
    ),
    Case(
        'set_last_read_position',
        'set_last_read_position',
        'sql',
        lambda c: c.set_last_read_position(A, 'FMT1', cfi='/2/4', epoch=1767225600.0, pos_frac=0.5),
        lambda c: true(c.get_last_read_positions(A, 'FMT1')),
    ),
    Case(
        'set_annotations_for_book',
        'set_annotations_for_book',
        'sql',
        lambda c: c.set_annotations_for_book(
            A, 'FMT1', [({'type': 'bookmark', 'title': 'new bookmark', 'pos': 'epubcfi(/4)', 'timestamp': '2026-02-01T00:00:00+00:00'}, 1769904000.0)]
        ),
        lambda c: eq(annotation_titles(c), ['new bookmark']),
        setup=old_annotations,
        state=annotation_titles,
    ),
    # }}}
    # Writing the backup of the metadata {{{
    Case('dump_metadata', 'dump_metadata', 'files', lambda c: c.dump_metadata([A]), lambda c: true(os.path.exists(os.path.join(c.backend.library_path, folder_name(c, A), 'metadata.opf')))),
    # }}}
)


# Known problems: (case id, fault id or pattern) -> (kinds of problems, description). Each entry below lists
# the case, the kinds of problems (see BaseTest.find_library_inconsistencies(), lost-file is data loss) and
# the faults after which they are found. The fault ids are described in fault_injection.py
KNOWN_INCONSISTENT = known_table(
    # DATA LOSS: the existing file is truncated (opened for writing) before the new contents are read, so a failure to read them destroys the old
    # contents
    ('add_extra_files_replace_unreadable', {'lost-file'}, (
        'self',
    )),
    # The in-memory formats are updated before the row of the data table is written, and the composite cache is cleared by update_last_modified(),
    # which is last
    ('add_format_new', {'field:formats', 'format-file-missing', 'formats'}, (
        'sql:insert or replace into data (book,format,uncompressed_size,name) values (?,?,?,?)@1',
    )),
    ('add_format_new', {'composite-cache'}, (
        'sql:update books set last_modified=? where id=?@1',
    )),
    # The file was replaced, but its new size was not recorded
    ('add_format_replace', {'format-size'}, (
        'file:backend.add_format@1/after',
    )),
    # DATA LOSS: as for extra files, the existing format file is truncated before the new contents are read
    ('add_format_replace_unreadable', {'format-size', 'lost-file'}, (
        'self',
    )),
    # The row of the book is inserted (and committed) before the metadata is set, if that fails the cache never learns of the book
    ('create_book_entry', {'ids'}, (
        'sql:update books set title=? where id=?@1',
        'sql:insert into authors(name,sort) values (?,?)@1',
        'sql:update books set path=? where id=?@1',
    )),
    ('create_book_entry', {'stray-cover'}, (
        'file:backend.set_cover@1/after',
        'sql:update books set has_cover=? where id=?@1',
    )),
    ('create_book_entry', {'field:timestamp'}, (
        'sql:update books set timestamp=? where id=?@1',
    )),
    ('create_book_entry', {'composite-cache', 'field:tags'}, (
        'sql:delete from books_tags_link where book=?@1',
        'sql:insert into books_tags_link(book,tag) values(?, ?)@1',
    )),
    # The note was written to the database but not its backup file
    ('import_note', {'note-backup-missing'}, (
        'file:notes.set_backup_for@1',
    )),
    # The cover file was written, but the book is not marked as having a cover
    ('merge_book_metadata', {'stray-cover'}, (
        'file:backend.set_cover@1/after',
    )),
    # As for create_book_entry, which it calls: the row is in the database but unknown to the cache, so the book cannot be restored again from the
    # trash, and a stray cover.jpg
    ('move_book_from_trash', {'ids'}, (
        'sql:update books set title=? where id=?@1',
        'sql:insert into authors(name,sort) values (?,?)@1',
        'sql:update books set path=? where id=?@1',
    )),
    ('move_book_from_trash', {'field:timestamp'}, (
        'sql:update books set timestamp=? where id=?@1',
    )),
    ('move_book_from_trash', {'stray-cover'}, (
        'file:copy_tree@1/after',
        'file:copier.delete_sources@1',
        'sql:update books set has_cover=? where id=?@1',
    )),
    ('move_book_from_trash', {'composite-cache', 'field:formats', 'format-file-missing', 'formats', 'stray-cover'}, (
        'sql:insert or replace into data (book,format,uncompressed_size,name) values (?,?,?,?)@1',
    )),
    # update_last_modified() is never called, so the cached composite columns that use formats are stale even when it works. And in-memory formats
    # are updated before the row of the data table is written
    ('move_format_from_trash', {'composite-cache'}, (
        'normal',
        'file:rmtree_with_retry@1',
    )),
    ('move_format_from_trash', {'field:formats', 'format-file-missing', 'formats'}, (
        'sql:insert or replace into data (book,format,uncompressed_size,name) values (?,?,?,?)@1',
    )),
    # Cleaning up the items that are no longer used (and their notes) is done after the books are removed, outside of the savepoint of each book,
    # and failures to remove an empty folder come after the rows were deleted but before the cache was updated
    ('remove_books', {'composite-cache', 'folder-missing', 'orphan-folder'}, (
        'file:copy_tree@1/after',
    )),
    ('remove_books', {'ids', 'orphan-folder'}, (
        'file:remove_dir_if_empty@1',
        'file:remove_dir_if_empty@2',
    )),
    ('remove_books', {'items:authors', 'note-orphan'}, (
        'sql:delete from notes_db.notes where id=?@1',
    )),
    ('remove_books', {'items:authors'}, (
        'file:notes.retire_entry@1',
        'file:notes.retire_entry@1/after',
        'sql:delete from authors where id=?@1',
    )),
    ('remove_books', {'items:tags'}, (
        'file:notes.retire_entry@2',
        'file:notes.remove_with_retry@1',
        'file:notes.remove_with_retry@2',
        'sql:delete from tags where id=?@1',
    )),
    # The format files are moved to the trash before the rows are deleted from the data table, so a failure leaves rows without files
    ('remove_formats', {'composite-cache', 'format-file-missing'}, (
        'file:atomic_write@2',
        'file:copy_files@1/after',
        'file:copy_files@2',
        'file:copier.copy_all@2',
        'file:copier.delete_sources@2',
    )),
    ('remove_formats', {'composite-cache', 'field:formats', 'formats'}, (
        'sql:delete from data where book=? and format=?@1',
    )),
    ('remove_formats', {'composite-cache'}, (
        'sql:update books set last_modified=? where id=?@1',
    )),
    # The in-memory formats are updated before the rows are deleted
    ('remove_formats_db_only', {'field:formats', 'formats'}, (
        'sql:delete from data where book=? and format=?@1',
    )),
    ('remove_formats_db_only', {'composite-cache'}, (
        'sql:update books set last_modified=? where id=?@1',
    )),
    # The in-memory tables are updated before the SQL statements are executed
    ('remove_items_custom', {'field:#tags', 'items:#tags'}, (
        'sql:delete from books_custom_column_7_link where value=?@1',
    )),
    ('remove_items_custom', {'items:#tags'}, (
        'sql:delete from custom_column_7 where id=?@1',
    )),
    # The in-memory tables are updated before the SQL statements, and the note of the item (a row and files) is removed first
    ('remove_items_tags', {'field:tags', 'items:tags', 'note-orphan'}, (
        'sql:delete from notes_db.notes where id=?@1',
    )),
    ('remove_items_tags', {'field:tags', 'items:tags'}, (
        'file:notes.retire_entry@1',
        'file:notes.retire_entry@1/after',
        'file:notes.remove_with_retry@1',
        'file:notes.remove_with_retry@2',
        'sql:delete from books_tags_link where tag=?@1',
    )),
    ('remove_items_tags', {'composite-cache', 'items:tags'}, (
        'sql:delete from tags where id=?@1',
    )),
    ('remove_items_tags', {'composite-cache'}, (
        'sql:update books set last_modified=? where id=?@1',
    )),
    # The authors are renamed (and committed) first, then the folders of the books are moved one at a time, failures leave some books in new
    # folders and others in old ones with inconsistent paths
    ('rename_items_authors', {'field:authors', 'items:authors'}, (
        'sql:update authors set name=? where id=?@1',
        'sql:update authors set name=? where id=?@2',
    )),
    ('rename_items_authors', {'composite-cache', 'folder-missing', 'orphan-folder'}, (
        'file:copy_tree@1/after',
        'sql:update data set name=? where book=? and format=?@1',
        'sql:update data set name=? where book=? and format=?@3',
    )),
    ('rename_items_authors', {'orphan-folder'}, (
        'file:copier.copy_all@1',
        'file:copier.copy_all@2',
        'file:copier.delete_sources@1',
        'file:copier.delete_sources@2',
        'file:remove_dir_if_empty@1',
        'file:remove_dir_if_empty@2',
    )),
    ('rename_items_authors', {'composite-cache', 'field:path', 'format-file-missing'}, (
        'sql:update books set path=? where id=?@1',
        'sql:update books set path=? where id=?@2',
    )),
    # The in-memory tables are updated before the SQL statement is executed
    ('rename_items_series', {'field:series', 'items:series'}, (
        'sql:update series set name=? where id=?@1',
    )),
    # The in-memory tables are updated before the SQL statement is executed
    ('rename_items_tags', {'field:tags', 'items:tags'}, (
        'sql:update tags set name=? where id=?@1',
    )),
    ('rename_items_tags', {'composite-cache'}, (
        'sql:update books set last_modified=? where id=?@1',
    )),
    # The note of the merged item is moved with several SQL statements and file operations that are not in one transaction
    ('rename_items_tags_merge', {'field:tags', 'items:tags', 'note-orphan'}, (
        'sql:delete from books_tags_link where book=? and tag=?@1',
    )),
    ('rename_items_tags_merge', {'composite-cache', 'field:tags', 'items:tags', 'note-orphan'}, (
        'file:notes.remove_with_retry@1',
        'sql:insert into notes_db.notes (item,colname,doc,searchable_text) values (?,?,?,?) returning n@1',
        'file:notes.set_backup_for@1/after',
        'sql:update books_tags_link set tag=? where tag=?;@1',
    )),
    ('rename_items_tags_merge', {'composite-cache', 'field:tags', 'items:tags', 'note-backup-missing', 'note-orphan'}, (
        'sql:insert into notes_db.notes_resources_link (note,resource) values (?,?);@1',
        'file:notes.set_backup_for@1',
    )),
    ('rename_items_tags_merge', {'composite-cache', 'items:tags', 'note-orphan'}, (
        'sql:delete from tags where id=?@1',
    )),
    ('rename_items_tags_merge', {'composite-cache'}, (
        'sql:update books set last_modified=? where id=?@1',
    )),
    # Moves the original over the format, then deletes the original with remove_formats(), the order of the steps leaves the sizes or rows of the
    # data table wrong
    ('restore_original_format', {'format-size'}, (
        'file:backend.add_format@1/after',
    )),
    ('restore_original_format', {'composite-cache', 'format-file-missing', 'format-size'}, (
        'file:atomic_rename@1/after',
    )),
    ('restore_original_format', {'field:formats', 'formats'}, (
        'sql:delete from data where book=? and format=?@1',
    )),
    # The saved search is stored in memory before it is written to the preferences
    ('saved_search_add', {'prefs'}, (
        'sql:update preferences set val=? where id=?@1',
    )),
    # The authors are changed (and committed) first, then the folder of the book is moved
    ('set_authors', {'field:authors'}, (
        'sql:delete from books_authors_link where book=?@1',
        'sql:insert into books_authors_link(book,author) values(?, ?)@1',
    )),
    ('set_authors', {'composite-cache', 'folder-missing', 'orphan-folder'}, (
        'file:copy_tree@1/after',
        'sql:update data set name=? where book=? and format=?@1',
    )),
    ('set_authors', {'orphan-folder'}, (
        'file:copier.copy_all@1',
        'file:copier.delete_sources@1',
        'file:remove_dir_if_empty@1',
    )),
    ('set_authors', {'composite-cache', 'field:path', 'format-file-missing'}, (
        'sql:update books set path=? where id=?@1',
    )),
    # The cover file is written before has_cover is updated
    ('set_cover_new', {'stray-cover'}, (
        'file:backend.set_cover@1/after',
        'sql:update books set has_cover=? where id=?@1',
    )),
    # The cover file is deleted before has_cover is updated
    ('set_cover_remove', {'cover-file-missing'}, (
        'file:backend.set_cover@1/after',
        'sql:update books set has_cover=? where id=?@1',
    )),
    # The cover files of all the books are written before any has_cover is updated
    ('set_cover_two_books', {'stray-cover'}, (
        'file:backend.set_cover@1/after',
        'file:backend.set_cover@2',
        'sql:update books set has_cover=? where id=?@1',
    )),
    # The in-memory tables are updated before the SQL statements are executed. The normal case is a bug: the series index is kept in memory when
    # the series changes, but the database resets it to 1.0 (see custom_series_index() in write.py)
    ('set_field_custom_series', {'field:#series_index'}, (
        'normal',
        'sql:delete from custom_column_4 where id=?@1',
        'sql:update books set last_modified=? where id=?@1',
    )),
    ('set_field_custom_series', {'field:#series'}, (
        'sql:delete from books_custom_column_4_link where book=?;@1',
        'sql:insert into books_custom_column_4_link(book,value,extra) values(?, ?, 1.0)@1',
    )),
    # The in-memory tables are updated before the SQL statements are executed
    ('set_field_custom_tags', {'field:#tags'}, (
        'sql:delete from books_custom_column_7_link where book=?@1',
        'sql:insert into books_custom_column_7_link(book,value) values(?, ?)@1',
    )),
    # The in-memory tables are updated before the SQL statements are executed
    ('set_field_identifiers', {'field:identifiers'}, (
        'sql:delete from identifiers where book=?@1',
        'sql:insert or replace into identifiers (book, type, val) values (?, ?, ?)@1',
    )),
    ('set_field_identifiers', {'composite-cache'}, (
        'sql:update books set last_modified=? where id=?@1',
    )),
    # The in-memory tables are updated before the SQL statements are executed
    ('set_field_rating', {'field:rating'}, (
        'sql:delete from books_ratings_link where book=?;@1',
        'sql:insert into books_ratings_link(book,rating) values(?, ?)@1',
    )),
    # The in-memory tables are updated before the SQL statements are executed
    ('set_field_series', {'field:series'}, (
        'sql:delete from books_series_link where book=?;@1',
        'sql:insert into books_series_link(book,series) values(?, ?)@1',
    )),
    # The in-memory tables are updated before the SQL statements are executed. Removing a tag that is no longer used removes its note first
    ('set_field_tags', {'field:tags'}, (
        'sql:delete from books_tags_link where book=?@1',
    )),
    ('set_field_tags', {'composite-cache', 'field:tags'}, (
        'sql:insert into books_tags_link(book,tag) values(?, ?)@1',
    )),
    ('set_field_tags', {'composite-cache'}, (
        'sql:delete from notes_db.notes where id=?@1',
        'file:notes.retire_entry@1',
        'file:notes.retire_entry@1/after',
        'file:notes.remove_with_retry@1',
        'file:notes.remove_with_retry@2',
        'sql:delete from tags where id=?@1',
        'sql:update books set last_modified=? where id=?@1',
    )),
    # The in-memory tables are updated before the SQL statement is executed
    ('set_link_for_authors', {'items:authors'}, (
        'sql:update authors set link=? where id=?@1',
    )),
    # The in-memory tables are updated before the SQL statement is executed
    ('set_link_map', {'items:tags'}, (
        'sql:update tags set link=? where id=?@1',
    )),
    # All the fields are set inside a transaction that is rolled back, with the in-memory tables reloaded, when something fails. But the files of
    # the note of a tag that is no longer used (backup, resources) are removed as it is retired, and these are not restored by the rollback
    ('set_metadata_fields', {'note-backup-missing'}, (
        'file:notes.retire_entry@1/after',
        'file:notes.remove_with_retry@1',
    )),
    ('set_metadata_fields', {'note-backup-missing', 'note-resource-missing'}, (
        'file:notes.remove_with_retry@2',
        'sql:delete from tags where id=?@1',
        'sql:insert or replace into comments(book,text) values (?,?)@1',
    )),
    # The title and authors are set, and the folder moved, before the other fields. The note of a tag that is no longer used is lost as for
    # set_metadata_fields
    ('set_metadata_rename', {'composite-cache', 'folder-missing', 'orphan-folder'}, (
        'file:copy_tree@1/after',
    )),
    ('set_metadata_rename', {'orphan-folder'}, (
        'file:copier.copy_all@1',
        'file:copier.delete_sources@1',
        'file:remove_dir_if_empty@1',
    )),
    ('set_metadata_rename', {'note-backup-missing'}, (
        'file:notes.retire_entry@1/after',
        'file:notes.remove_with_retry@1',
    )),
    ('set_metadata_rename', {'note-backup-missing', 'note-resource-missing'}, (
        'file:notes.remove_with_retry@2',
    )),
    # The note is written in several steps that are not in a transaction, the backup file is written last
    ('set_notes_new', {'note-backup-missing'}, (
        'sql:insert into notes_db.notes_resources_link (note,resource) values (?,?);@1',
        'file:notes.set_backup_for@1',
    )),
    # The in-memory tables are updated before the SQL statement is executed
    ('set_sort_for_authors', {'items:authors'}, (
        'sql:update authors set sort=? where id=?@1',
    )),
    # Titles are written, then the folders of the books are moved one at a time, failures leave some books in new folders and others in old ones,
    # and the sort field is written after the title
    ('set_title_two_books', {'field:sort'}, (
        'sql:update books set sort=? where id=?@1',
        'sql:update books set sort=? where id=?@2',
    )),
    ('set_title_two_books', {'composite-cache', 'folder-missing', 'orphan-folder'}, (
        'file:copy_tree@1/after',
        'sql:update data set name=? where book=? and format=?@1',
        'sql:update data set name=? where book=? and format=?@3',
    )),
    ('set_title_two_books', {'orphan-folder'}, (
        'file:copier.copy_all@1',
        'file:copier.copy_all@2',
        'file:copier.delete_sources@1',
        'file:copier.delete_sources@2',
    )),
    ('set_title_two_books', {'composite-cache', 'field:path', 'format-file-missing'}, (
        'sql:update books set path=? where id=?@1',
        'sql:update books set path=? where id=?@2',
    )),
    # The note is restored in several steps that are not in a transaction, the backup file is written last
    ('unretire_note_for', {'note-backup-missing'}, (
        'sql:insert into notes_db.notes_resources_link (note,resource) values (?,?);@1',
        'file:notes.set_backup_for@1',
    )),
    # The folder of the book is copied to the new name, the sources deleted, and only then are the rows of the data and books tables updated
    ('update_path', {'composite-cache', 'folder-missing', 'orphan-folder'}, (
        'file:copy_tree@1/after',
        'sql:update data set name=? where book=? and format=?@1',
    )),
    ('update_path', {'orphan-folder'}, (
        'file:copier.copy_all@1',
        'file:copier.delete_sources@1',
    )),
    ('update_path', {'composite-cache', 'field:path', 'format-file-missing'}, (
        'sql:update books set path=? where id=?@1',
    )),
)


@add_case_tests
class WriteConsistencyTest(FaultInjectionTest):
    CASES = CASES
    KNOWN_INCONSISTENT = KNOWN_INCONSISTENT

    @classmethod
    def tearDownClass(cls):
        if TABLE_PATH:
            write_table(FaultInjectionTest.results, TABLE_PATH)

    def test_template_library(self):
        "The library on which the cases operate is consistent"
        from calibre.db.tests.fault_injection import template_library

        cache = self.init_cache(self.clone_library(template_library(self)))
        self.assert_library_consistent(cache)
        eq(sorted(cache.all_book_ids()), [A, B, C])
        eq(cache.formats(A), ('FMT1', 'FMT2'))
        eq({x.book_id for x in cache.list_trash_entries()[0]}, {D})
        eq({x.book_id for x in cache.list_trash_entries()[1]}, {B})
