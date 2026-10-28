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
        archive_size=file.seek(0,2)
        if archive_size<32:raise ValueError('Archive is truncated')
        file.seek(0)
        if file.read(8)!=MAGIC:raise ValueError('Not a supported MKVA archive')
        file.seek(-24,2);offset,size,magic=struct.unpack('<QQ8s',file.read(24))
        if magic!=MAGIC or not 0<size<10000000 or offset<8 or offset+size!=archive_size-24:
            raise ValueError('Invalid archive catalogue bounds')
        file.seek(offset);catalog=json.loads(file.read(size))
        if catalog.get('format')!=1 or not isinstance(catalog.get('entries'),list):
            raise ValueError('Unsupported archive catalogue')
        records=catalog['entries']+catalog.get('attachments',[])
        ids=set()
        for entry in records:
            if not isinstance(entry.get('id'),str) or not entry['id'] or entry['id'] in ids:
                raise ValueError('Invalid or duplicate archive entry ID')
            ids.add(entry['id'])
            if (any(type(entry.get(k)) is not int or entry[k]<0 for k in
                    ('size','map_offset','map_compressed_bytes','extent_count'))
                    or entry['extent_count']>3000000 or entry['map_offset']<8
                    or entry['map_offset']+entry['map_compressed_bytes']>offset
                    or not isinstance(entry.get('sha256'),str) or len(entry['sha256'])!=64):
                raise ValueError('Invalid archive entry bounds')
        return catalog

def read_map(file,entry):
    expected=entry['extent_count']*24
    if not 0<=expected<=72000000:raise ValueError('Archive extent map is too large')
    file.seek(entry['map_offset']);decoder=zlib.decompressobj()
    raw=decoder.decompress(file.read(entry['map_compressed_bytes']),expected+1)
    if len(raw)!=expected or not decoder.eof or decoder.unused_data:
        raise ValueError('Invalid archive extent map')
    end=file.seek(0,2)-24;logical_end=0
    spans=list(TRIPLE.iter_unpack(raw))
    for logical,physical,length in spans:
        if logical!=logical_end or length<=0 or physical<8 or physical+length>min(end,entry['map_offset']):
            raise ValueError('Invalid archive media extent')
        logical_end+=length
    if logical_end!=entry['size']:raise ValueError('Archive entry size differs from extent map')
    return spans

def verify_view(archive,entry):
    digest=hashlib.sha256();total=0
    # Extents jump backwards: avoid refilling a 4 MiB buffer for tiny reads.
    with open(archive,'rb',buffering=0) as file:
        spans=list(read_map(file,entry))
        for logical,physical,length in spans:
            if logical!=total:raise ValueError('Non-contiguous archive view')
            file.seek(physical)
            remaining=length
            while remaining:
                data=file.read(min(remaining,4*1024*1024))
                if not data:raise EOFError('Truncated archive payload')
                digest.update(data);remaining-=len(data)
            total+=length
    if total!=entry['size'] or digest.hexdigest()!=entry['sha256']:
        raise ValueError('Reconstructed view checksum failed')
    return digest.hexdigest()

def publish_new(partial,output):
    # Windows rename refuses an existing target; POSIX needs an exclusive link.
    if os.name=='nt':os.rename(partial,output)
    else:
        os.link(partial,output)
        os.unlink(partial)


def extract(archive,cut,output):
    archive=Path(archive);output=Path(output)
    catalog=index_of(archive)
    entry=next((e for e in catalog['entries']+catalog.get('attachments',[]) if e['id']==cut),None)
    if entry is None:raise ValueError('Unknown archive entry ID')
    partial=output.with_suffix(output.suffix+'.partial')
    if output.exists() or partial.exists():raise FileExistsError('Never overwrite an existing file')
    output.parent.mkdir(parents=True,exist_ok=True)
    checksum=hashlib.sha256()
    with archive.open('rb',buffering=4*1024*1024) as source,partial.open('xb',buffering=4*1024*1024) as target:
        spans=list(read_map(source,entry))
        for logical,physical,length in spans:
            if target.tell()!=logical:raise ValueError('Non-contiguous recovery output')
            source.seek(physical)
            while length:
                block=source.read(min(length,4*1024*1024))
                if not block:raise EOFError('Truncated archive payload')
                target.write(block);checksum.update(block);length-=len(block)
    if checksum.hexdigest()!=entry['sha256']:raise ValueError('Restored file checksum failed; partial preserved')
    publish_new(partial,output)
    print(json.dumps({'restored':str(output),'sha256':checksum.hexdigest(),'byte_exact':True}))

def build(output,inputs,attachments=None):
    output=Path(output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists() or output.with_suffix('.verification.json').exists():
        raise FileExistsError('Never overwrite an existing archive or verification report')
    attachments=attachments or []
    all_specs=list(inputs)+list(attachments)
    if not inputs or len({s['id'] for s in all_specs})!=len(all_specs):
        raise ValueError('Archive inputs need unique entry IDs')
    partial=output.with_suffix(output.suffix+'.partial')
    if partial.exists():raise FileExistsError('Partial archive already exists; inspect it before retrying')
    if shutil.disk_usage(output.parent).free<=sum(Path(x['path']).stat().st_size for x in all_specs)+1024**3:
        raise ValueError('Insufficient free space to build and verify this archive')
    objects={};entries=[];stored_attachments=[];reused=0;physical_end=8
    with open(partial,'w+b',buffering=4*1024*1024) as archive:
        archive.write(MAGIC)
        for number,spec in enumerate(all_specs):
            path=Path(spec['path']).resolve();size=path.stat().st_size
            extents=bytearray();logical=0;source_hash=hashlib.sha256();last_report=time.monotonic()
            with open(path,'rb',buffering=4*1024*1024) as parser,open(path,'rb',buffering=4*1024*1024) as source:
                def emit(start,length,deduplicate):
                    nonlocal logical,physical_end,reused,last_report
                    if start!=logical:raise ValueError('Non-contiguous source payload')
                    source.seek(start)
                    remaining=length
                    while remaining:
                        block=source.read(min(remaining,8*1024*1024))
                        if not block:raise EOFError('Source file changed or was truncated during packing')
                        source_hash.update(block)
                        key=hashlib.sha256(block).digest() if deduplicate and len(block)>128 else None
                        existing=objects.get(key) if key is not None else None
                        if existing is not None:
                            physical,count=existing
                            if count!=len(block):raise ValueError('Shared payload size mismatch')
                            reused+=len(block)
                        else:
                            physical=physical_end;archive.seek(physical);archive.write(block);physical_end+=len(block)
                            if key is not None:objects[key]=(physical,len(block))
                        extents.extend(TRIPLE.pack(logical,physical,len(block)))
                        logical+=len(block);remaining-=len(block)
                    if time.monotonic()-last_report>8:
                        print(json.dumps({'cut':spec['id'],'percent':round(logical/size*100,1),'shared_GiB':round(reused/1024**3,2)}),flush=True)
                        last_report=time.monotonic()
                for start,length in (payloads(parser,size) if number<len(inputs) else []):
                    if start>logical:emit(logical,start-logical,False)
                    emit(start,length,True)
                if logical<size:emit(logical,size-logical,False)
            checksum=source_hash.hexdigest()
            if spec.get('expected_sha256') and checksum!=spec['expected_sha256']:
                raise ValueError('Source differs from approved checksum')
            blob=zlib.compress(extents,6);map_offset=physical_end
            archive.seek(map_offset);archive.write(blob);physical_end+=len(blob)
            entry={k:v for k,v in spec.items() if k not in ('path','expected_sha256')}
            entry.update(name=path.name,size=size,sha256=checksum,map_offset=map_offset,map_compressed_bytes=len(blob),extent_count=len(extents)//24)
            (entries if number<len(inputs) else stored_attachments).append(entry)
            print(json.dumps({'cut':spec['id'],'indexed':True,'extents':entry['extent_count']}),flush=True)
        index={'format':1,'entries':entries,'combined_source_bytes':sum(e['size'] for e in entries),'reused_payload_bytes':reused}
        if stored_attachments:index['attachments']=stored_attachments
        raw=json.dumps(index,separators=(',',':')).encode();index_offset=physical_end
        archive.seek(index_offset);archive.write(raw);archive.write(struct.pack('<QQ8s',index_offset,len(raw),MAGIC));archive.flush();os.fsync(archive.fileno())
    for entry in entries+stored_attachments:
        verify_view(partial,entry)
        print(json.dumps({'cut':entry['id'],'byte_exact_reconstruction_verified':True}),flush=True)
    publish_new(partial,output)
    result={'archive':str(output),'archive_bytes':output.stat().st_size,'original_bytes':index['combined_source_bytes'],'saved_bytes':index['combined_source_bytes']-output.stat().st_size,
            'entries':[{'id':e['id'],'size':e['size'],'sha256':e['sha256']} for e in entries],
            'attachment_count':len(stored_attachments),'all_views_byte_exact':True}
    output.with_suffix('.verification.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2),flush=True)
    return result

if __name__=='__main__':
    import sys
    if len(sys.argv)>1 and sys.argv[1]=='extract':
        if len(sys.argv)!=5:raise SystemExit('Usage: extract ARCHIVE CUT OUTPUT')
        extract(sys.argv[2],sys.argv[3],sys.argv[4]);raise SystemExit(0)
    parser=argparse.ArgumentParser();parser.add_argument('spec');parser.add_argument('output');args=parser.parse_args()
    build(args.output,json.loads(Path(args.spec).read_text()))
