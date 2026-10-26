"""MKVA (MKV Archive): lossless shared storage for multiple original MKVs.

Use .mkva for new archives. Legacy .jfshare files have the same v1 layout.

Only EBML Block/SimpleBlock encoded payloads are deduplicated. All headers, cue
indexes, timestamps and track metadata are preserved as exact per-file byte views.
Format: magic, payload objects, zlib extent maps, JSON index, <index offset,size,magic>.
Extent triples are virtual offset, physical archive offset and length (little endian u64).
"""
import argparse, hashlib, json, os, shutil, struct, time, zlib
from pathlib import Path

# Stable v1 signature: the extension rename does not change archive bytes.
MAGIC=b'JFSHARE1'
TRIPLE=struct.Struct('<QQQ')

def vint(file,identifier=False):
    first=file.read(1)
    if not first:raise EOFError('Truncated EBML integer')
    marker=0x80; size=1
    while not first[0]&marker:
        marker>>=1;size+=1
        if size>8:raise ValueError('Invalid EBML integer')
    raw=first+file.read(size-1)
    if len(raw)!=size:raise EOFError('Truncated EBML integer')
    value=int.from_bytes(raw,'big')
    if identifier:return value,size
    value&=(1<<(7*size))-1
    return (None if value==(1<<(7*size))-1 else value),size

def payloads(file,end,level=0):
    """Yield ordered encoded-payload byte spans; do not descend into codec bytes."""
    while file.tell()<end:
        identifier,_=vint(file,True);size,_=vint(file)
        start=file.tell(); stop=end if size is None else start+size
        if stop>end or stop<start:raise ValueError('EBML element outside parent')
        if identifier in (0x18538067,0x1f43b675,0xa0):
            if level>4:raise ValueError('Unexpected nested media structure')
            yield from payloads(file,stop,level+1)
        elif identifier in (0xa3,0xa1):
            _,track_length=vint(file)
            header=track_length+3
            if size is None or size<header:raise ValueError('Invalid media block')
            yield start+header,size-header
        file.seek(stop)
    if file.tell()!=end:raise ValueError('EBML alignment failure')

def index_of(path):
    with open(path,'rb') as file:
        assert file.read(8)==MAGIC
        file.seek(-24,2);offset,size,magic=struct.unpack('<QQ8s',file.read(24))
        assert magic==MAGIC and size<10000000
        file.seek(offset);return json.loads(file.read(size))

def read_map(file,entry):
    file.seek(entry['map_offset']);raw=zlib.decompress(file.read(entry['map_compressed_bytes']))
    assert len(raw)==entry['extent_count']*24
    return TRIPLE.iter_unpack(raw)

def verify_view(archive,entry):
    digest=hashlib.sha256();total=0
    # Extents jump backwards: avoid refilling a 4 MiB buffer for tiny reads.
    with open(archive,'rb',buffering=0) as file:
        spans=list(read_map(file,entry))
        for logical,physical,length in spans:
            assert logical==total
            file.seek(physical)
            remaining=length
            while remaining:
                data=file.read(min(remaining,4*1024*1024));assert data
                digest.update(data);remaining-=len(data)
            total+=length
    assert total==entry['size'] and digest.hexdigest()==entry['sha256']
    return digest.hexdigest()

def extract(archive,cut,output):
    archive=Path(archive);output=Path(output)
    entry=next(e for e in index_of(archive)['entries'] if e['id']==cut)
    partial=output.with_suffix(output.suffix+'.partial')
    assert not output.exists() and not partial.exists(),'Never overwrite an existing file'
    output.parent.mkdir(parents=True,exist_ok=True)
    checksum=hashlib.sha256()
    with archive.open('rb',buffering=4*1024*1024) as source,partial.open('xb',buffering=4*1024*1024) as target:
        spans=list(read_map(source,entry))
        for logical,physical,length in spans:
            assert target.tell()==logical
            source.seek(physical)
            while length:
                block=source.read(min(length,4*1024*1024));assert block
                target.write(block);checksum.update(block);length-=len(block)
    assert checksum.hexdigest()==entry['sha256'],'Restored media checksum failed'
    partial.rename(output)
    print(json.dumps({'restored':str(output),'sha256':checksum.hexdigest(),'byte_exact':True}))

def build(output,inputs):
    output=Path(output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    assert not output.exists(),'Never overwrite an existing archive'
    partial=output.with_suffix(output.suffix+'.partial');assert not partial.exists()
    assert shutil.disk_usage(output.parent).free>sum(Path(x['path']).stat().st_size for x in inputs)+1024**3
    objects={};entries=[];reused=0;physical_end=8
    with open(partial,'w+b',buffering=4*1024*1024) as archive:
        archive.write(MAGIC)
        for spec in inputs:
            path=Path(spec['path']).resolve();size=path.stat().st_size
            extents=bytearray();logical=0;source_hash=hashlib.sha256();last_report=time.monotonic()
            with open(path,'rb',buffering=4*1024*1024) as parser,open(path,'rb',buffering=4*1024*1024) as source:
                def emit(start,length,deduplicate):
                    nonlocal logical,physical_end,reused,last_report
                    assert start==logical
                    source.seek(start)
                    remaining=length
                    while remaining:
                        block=source.read(min(remaining,8*1024*1024));assert block
                        source_hash.update(block)
                        key=hashlib.sha256(block).digest() if deduplicate and len(block)>128 else None
                        existing=objects.get(key) if key is not None else None
                        if existing is not None:
                            physical,count=existing;assert count==len(block);reused+=len(block)
                        else:
                            physical=physical_end;archive.seek(physical);archive.write(block);physical_end+=len(block)
                            if key is not None:objects[key]=(physical,len(block))
                        extents.extend(TRIPLE.pack(logical,physical,len(block)))
                        logical+=len(block);remaining-=len(block)
                    if time.monotonic()-last_report>8:
                        print(json.dumps({'cut':spec['id'],'percent':round(logical/size*100,1),'shared_GiB':round(reused/1024**3,2)}),flush=True)
                        last_report=time.monotonic()
                for start,length in payloads(parser,size):
                    if start>logical:emit(logical,start-logical,False)
                    emit(start,length,True)
                if logical<size:emit(logical,size-logical,False)
            checksum=source_hash.hexdigest()
            if spec.get('expected_sha256'):assert checksum==spec['expected_sha256'],'Source differs from approved Ready file'
            blob=zlib.compress(extents,6);map_offset=physical_end
            archive.seek(map_offset);archive.write(blob);physical_end+=len(blob)
            entry={k:v for k,v in spec.items() if k not in ('path','expected_sha256')}
            entry.update(name=path.name,size=size,sha256=checksum,map_offset=map_offset,map_compressed_bytes=len(blob),extent_count=len(extents)//24)
            entries.append(entry)
            print(json.dumps({'cut':spec['id'],'indexed':True,'extents':entry['extent_count']}),flush=True)
        index={'format':1,'entries':entries,'combined_source_bytes':sum(e['size'] for e in entries),'reused_payload_bytes':reused}
        raw=json.dumps(index,separators=(',',':')).encode();index_offset=physical_end
        archive.seek(index_offset);archive.write(raw);archive.write(struct.pack('<QQ8s',index_offset,len(raw),MAGIC));archive.flush();os.fsync(archive.fileno())
    for entry in entries:
        verify_view(partial,entry)
        print(json.dumps({'cut':entry['id'],'byte_exact_reconstruction_verified':True}),flush=True)
    partial.rename(output)
    result={'archive':str(output),'archive_bytes':output.stat().st_size,'original_bytes':index['combined_source_bytes'],'saved_bytes':index['combined_source_bytes']-output.stat().st_size,
            'entries':[{'id':e['id'],'size':e['size'],'sha256':e['sha256']} for e in entries],'all_views_byte_exact':True}
    output.with_suffix('.verification.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2),flush=True)
    return result

if __name__=='__main__':
    import sys
    if len(sys.argv)>1 and sys.argv[1]=='extract':
        assert len(sys.argv)==5,'extract ARCHIVE CUT OUTPUT'
        extract(sys.argv[2],sys.argv[3],sys.argv[4]);raise SystemExit(0)
    parser=argparse.ArgumentParser();parser.add_argument('spec');parser.add_argument('output');args=parser.parse_args()
    build(args.output,json.loads(Path(args.spec).read_text()))
