"""Codec-shaped synthetic packets exercise recovery; no media files are included."""
import contextlib
import io
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import mkva_archive as archive
import mkva_tool as tool


def vint(n):
    for count in range(1, 9):
        if n < (1 << (7*count))-1:
            return (n | (1 << (7*count))).to_bytes(count, 'big')


def element(identifier, data):
    return identifier + vint(len(data)) + data


def movie(cut):
    info = element(b'\x15\x49\xa9\x66', element(b'\x44\x89', struct.pack('>d', 60000.0)))
    blocks = b''
    for track, payload in [(1, b'\x00\x00\x01\x00' + b'picture'*700),
                           (2, b'\x0b\x77' + b'audio'*500),
                           (3, b'bitmap subtitles'*80),
                           (3, b'cut subtitles ' + bytes([cut])*300)]:
        blocks += element(b'\xa3', bytes([0x80+track, 0, cut, 0x80]) + payload)
    return element(b'\x18\x53\x80\x67', info + element(b'\x1f\x43\xb6\x75', blocks))


class DesktopToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source'; self.source.mkdir()
        for i, name in enumerate(['Film - Directors Cut', 'Film - Theatrical']):
            (self.source/(name+'.mkv')).write_bytes(movie(i+1))
            for extension in ['.nfo', '.en.forced.srt', '.en.idx', '.en.sub']:
                (self.source/(name+extension)).write_bytes(bytes([i+1])*200)
        (self.source/'poster.jpg').write_bytes(b'art')
        self.output = self.root/'Film.mkva'

    def pack(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return tool.pack_folder(self.source, self.output)

    def test_duration_is_included_for_existing_plugin(self):
        self.pack()
        catalog = archive.index_of(self.output)
        self.assertTrue(all(e['duration_ticks'] == 600_000_000 for e in catalog['entries']))
        self.assertEqual([e['label'] for e in catalog['entries']], ['Directors Cut', 'Theatrical'])

    def test_full_and_selected_recovery_preserves_subtitles_and_metadata(self):
        original = {p.name:p.read_bytes() for p in self.source.iterdir()}
        result = self.pack(); self.assertGreater(result['saved_bytes'], 0)
        with contextlib.redirect_stdout(io.StringIO()):
            tool.unpack(self.output, self.root/'all')
            tool.unpack(self.output, self.root/'one', ['edition-1'])
        self.assertEqual({p.name:p.read_bytes() for p in (self.root/'all').iterdir()}, original)
        self.assertEqual(len(list((self.root/'one').iterdir())), 6)
        self.assertFalse(any('Theatrical' in p.name for p in (self.root/'one').iterdir()))
        self.assertEqual({p.name:p.read_bytes() for p in self.source.iterdir()}, original)

    def test_existing_folder_is_never_overwritten(self):
        self.pack()
        with self.assertRaises(FileExistsError): tool.unpack(self.output, self.source)

    def test_missing_duration_is_rejected_before_pack(self):
        (self.source/'Film - Theatrical.mkv').write_bytes(element(b'\x18\x53\x80\x67', b''))
        with self.assertRaisesRegex(ValueError, 'duration is missing'): self.pack()
        self.assertFalse(self.output.exists())

    def test_unsafe_output_names_are_rejected(self):
        for name in ['../x', 'C:\\x', 'CON', 'NUL.txt', 'x.', 'a\x00b']:
            with self.subTest(name=name), self.assertRaises(ValueError): tool.safe_name(name)

    def test_corrupt_archive_never_publishes_recovered_file(self):
        self.pack()
        entry = archive.index_of(self.output)['entries'][0]
        with self.output.open('r+b') as f: f.seek(8); f.write(b'X')
        target = self.root/'bad.mkv'
        with self.assertRaises(ValueError): archive.extract(self.output, entry['id'], target)
        self.assertFalse(target.exists())


if __name__ == '__main__': unittest.main()
