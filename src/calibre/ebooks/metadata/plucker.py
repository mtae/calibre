# License: GPLv3 Copyright: 2009, John Schember <john@nachtimwald.com>

"""
Read meta information from Plucker pdb files.
"""

import struct
from datetime import datetime

from calibre.ebooks.metadata import MetaInformation
from calibre.ebooks.pdb.header import PdbHeaderReader
from calibre.ebooks.pdb.plucker.reader import DATATYPE_METADATA, MIBNUM_TO_NAME, SectionHeader
from calibre.utils.date import local_tz
from calibre.utils.localization import _


def get_metadata(stream, extract_cover=True):
    """
    Return metadata as a L{MetaInfo} object
    """
    mi = MetaInformation(_('Unknown'), [_('Unknown')])
    stream.seek(0)

    pheader = PdbHeaderReader(stream)
    section_data = None
    for i in range(1, pheader.num_sections):
        raw_data = pheader.section_data(i)
        section_header = SectionHeader(raw_data)
        if section_header.type == DATATYPE_METADATA:
            section_data = raw_data[8:]
            break

    if not section_data:
        return mi

    default_encoding = 'latin-1'
    (record_count,) = struct.unpack('>H', section_data[0:2])
    adv = 0
    title = None
    author = None
    pubdate = None
    for i in range(record_count):
        try:
            type, length = struct.unpack_from('>HH', section_data, 2 + adv)
        except struct.error:
            break

        # CharSet
        if type == 1:
            (val,) = struct.unpack('>H', section_data[6 + adv : 8 + adv])
            default_encoding = MIBNUM_TO_NAME.get(val, 'latin-1')
        # Author
        elif type == 4:
            author = section_data[6 + adv : 2 + adv + 2 * length]
        # Title
        elif type == 5:
            title = section_data[6 + adv : 2 + adv + 2 * length]
        # Publication Date
        elif type == 6:
            (pubdate,) = struct.unpack('>I', section_data[6 + adv : 6 + adv + 4])

        adv += 2 * length

    if title:
        mi.title = title.replace(b'\0', b'').decode(default_encoding, 'replace')
    if author:
        author = author.replace(b'\0', b'').decode(default_encoding, 'replace')
        mi.authors = author.split(',')
    if pubdate is not None:
        mi.pubdate = datetime.fromtimestamp(pubdate, tz=local_tz)

    return mi


def find_tests():
    import unittest
    from io import BytesIO

    def build_pdb(records):
        meta = b''
        for rtype, data in records:
            data += b'\0' * (len(data) % 2)
            meta += struct.pack('>HH', rtype, 2 + len(data) // 2) + data
        meta = struct.pack('>H', len(records)) + meta
        section = struct.pack('>HHHBB', 0, 0, len(meta), DATATYPE_METADATA, 0) + meta
        header = bytearray(78)
        header[60:68] = b'DataPlkr'
        header[76:78] = struct.pack('>H', 2)
        header += struct.pack('>LBBBB', 94, 0, 0, 0, 0) + struct.pack('>LBBBB', 94, 0, 0, 0, 1)
        return BytesIO(bytes(header) + section)

    class Test(unittest.TestCase):
        def test_plucker_metadata(self):
            pubdate = 86400 * 365
            stream = build_pdb([
                (1, struct.pack('>H', 106)),
                (4, 'Ä Author'.encode()),
                (5, 'Tést\0'.encode()),
                (6, struct.pack('>I', pubdate)),
            ])
            mi = get_metadata(stream)
            self.assertEqual(mi.title, 'Tést')
            self.assertEqual(mi.authors, ['Ä Author'])
            self.assertEqual(int(mi.pubdate.timestamp()), pubdate)

        def test_plucker_no_pubdate(self):
            mi = get_metadata(build_pdb([(5, b'Test\0')]))
            self.assertEqual(mi.title, 'Test')
            self.assertIsNone(mi.pubdate)

    return unittest.defaultTestLoader.loadTestsFromTestCase(Test)
