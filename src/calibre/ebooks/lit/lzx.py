# License: GPLv3 Copyright: 2008, Marshall T. Vandegrift <llasram@gmail.com>

"""
LZX compression/decompression wrapper.
"""

from calibre_extensions import lzx as _lzx

__all__ = ['Compressor', 'Decompressor', 'LZXError']

LZXError = _lzx.LZXError
Compressor = _lzx.Compressor


class Decompressor:
    def __init__(self, wbits):
        self.wbits = wbits
        self.blocksize = 1 << wbits
        _lzx.init(wbits)

    def decompress(self, data, outlen):
        return _lzx.decompress(data, outlen)

    def reset(self):
        return _lzx.reset()


def find_tests():
    import unittest

    class Test(unittest.TestCase):
        def test_lzx_empty_huffman_table(self):
            # A verbatim block whose pretree has all code lengths zero
            bits = '0' + '001' + f'{64:024b}' + '0' * 80 + '0' * 64
            bits += '0' * (-len(bits) % 16)
            data = b''.join(int(bits[i : i + 16], 2).to_bytes(2, 'little') for i in range(0, len(bits), 16))
            self.assertRaises(LZXError, Decompressor(15).decompress, data, 64)

    return unittest.defaultTestLoader.loadTestsFromTestCase(Test)
