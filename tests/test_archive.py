"""Byte-layout tests use small synthetic EBML blocks, not codec/playback fixtures."""
import contextlib
import hashlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import mkva_archive as archive


def vint(value):
    for size in range(1,9):
        if value<(1<<(7*size))-1:
            return (value|(1<<(7*size))).to_bytes(size,'big')
    raise ValueError('oversized fixture')


def fixture(last):
    blocks=[]
    for index in list(range(12))+[last]:
        body=b'\x81\0\0\x80'+bytes([index])*8192
        blocks.append(b'\xa3'+vint(len(body))+body)
    data=b''.join(blocks)
    cluster=b'\x1f\x43\xb6\x75'+vint(len(data))+data
    return b'\x18\x53\x80\x67'+vint(len(cluster))+cluster


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.inputs=[]
        for identity,label,last in [('theatrical','Theatrical',100),('extended','Extended Edition',101)]:
            path=self.root/(identity+'.mkv');path.write_bytes(fixture(last))
            self.inputs.append({'id':identity,'label':label,'path':str(path),'duration_ticks':100_000_000,
                                'expected_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        self.output=self.root/'film.mkva'

    def pack(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return archive.build(self.output,self.inputs)

    def test_each_edition_recovers_exactly_and_payloads_are_shared(self):
        receipt=self.pack()
        self.assertTrue(receipt['all_views_byte_exact'])
        self.assertGreater(receipt['saved_bytes'],0)
        catalog=archive.index_of(self.output)
        self.assertEqual([x['id'] for x in catalog['entries']],['theatrical','extended'])
        for entry in catalog['entries']:
            self.assertEqual(archive.verify_view(self.output,entry),entry['sha256'])
            recovered=self.root/('recovered-'+entry['id']+'.mkv')
            with contextlib.redirect_stdout(io.StringIO()):
                archive.extract(self.output,entry['id'],recovered)
            original=next(x for x in self.inputs if x['id']==entry['id'])
            self.assertEqual(recovered.read_bytes(),Path(original['path']).read_bytes())

    def test_legacy_extension_uses_identical_bytes_and_index(self):
        self.pack()
        legacy=self.root/'film.jfshare';shutil.copy2(self.output,legacy)
        self.assertEqual(legacy.read_bytes(),self.output.read_bytes())
        self.assertEqual(archive.index_of(legacy),archive.index_of(self.output))
        for entry in archive.index_of(legacy)['entries']:
            self.assertEqual(archive.verify_view(legacy,entry),entry['sha256'])

    def test_existing_archive_and_recovery_outputs_are_preserved(self):
        self.pack();before=self.output.read_bytes()
        with self.assertRaises(AssertionError):
            archive.build(self.output,self.inputs)
        self.assertEqual(self.output.read_bytes(),before)
        destination=self.root/'existing.mkv';destination.write_bytes(b'user data')
        with self.assertRaises(AssertionError):
            archive.extract(self.output,'theatrical',destination)
        self.assertEqual(destination.read_bytes(),b'user data')

    def test_wrong_approved_source_hash_does_not_publish(self):
        self.inputs[0]['expected_sha256']='0'*64
        with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(AssertionError):
            archive.build(self.output,self.inputs)
        self.assertFalse(self.output.exists())
        self.assertTrue(self.output.with_suffix('.mkva.partial').exists())
        self.assertEqual(Path(self.inputs[0]['path']).read_bytes(),fixture(100))

    def test_invalid_header_or_footer_is_rejected(self):
        self.pack();data=self.output.read_bytes()
        for position in [0,len(data)-1]:
            bad=bytearray(data);bad[position]^=1
            corrupted=self.root/('corrupt-'+str(position)+'.mkva');corrupted.write_bytes(bad)
            with self.assertRaises(AssertionError):
                archive.index_of(corrupted)


if __name__=='__main__':
    unittest.main()
