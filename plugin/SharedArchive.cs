using System.Buffers.Binary;
using System.IO.Compression;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Net.Http.Headers;

namespace SharedEditions;

public sealed record Extent(long Logical,long Physical,long Length);
public sealed record ArchiveEntry(string Id,string Label,long Size,string Sha256,long DurationTicks,Extent[] Extents);

// MKVA v1 retains the JFSHARE1 signature for compatibility with existing archives.
// Both .mkva and legacy .jfshare names use the same byte-exact archive layout.
public sealed class SharedArchive
{
    private static SharedArchive? _instance;
    private static readonly object Sync=new();
    public string Path { get; }
    public IReadOnlyDictionary<string,ArchiveEntry> Entries { get; }
    public static SharedArchive? Configured
    {
        get
        {
            var path=Environment.GetEnvironmentVariable("SHARED_EDITIONS_ARCHIVE");
            if(string.IsNullOrEmpty(path)) return null;
            lock(Sync) return _instance ??= new SharedArchive(path);
        }
    }
    internal SharedArchive(string path)
    {
        Path=System.IO.Path.GetFullPath(path);
        using var file=new FileStream(Path,FileMode.Open,FileAccess.Read,FileShare.Read);
        var magic=new byte[8];file.ReadExactly(magic);
        if(Encoding.ASCII.GetString(magic)!="JFSHARE1" || file.Length<32) throw new InvalidDataException("Not a shared archive");
        file.Seek(-24,SeekOrigin.End);var footer=new byte[24];file.ReadExactly(footer);
        long indexOffset=checked((long)BinaryPrimitives.ReadUInt64LittleEndian(footer));
        long indexLength=checked((long)BinaryPrimitives.ReadUInt64LittleEndian(footer.AsSpan(8)));
        if(Encoding.ASCII.GetString(footer,16,8)!="JFSHARE1" || indexLength is < 1 or > 10000000 || indexOffset<8 || indexOffset!=file.Length-24-indexLength) throw new InvalidDataException("Invalid archive index");
        file.Position=indexOffset;var index=new byte[indexLength];file.ReadExactly(index);
        using var json=JsonDocument.Parse(index);
        if(json.RootElement.GetProperty("format").GetInt32()!=1) throw new InvalidDataException("Unsupported archive version");
        var entries=new Dictionary<string,ArchiveEntry>(StringComparer.Ordinal);
        int totalExtents=0;
        foreach(var item in json.RootElement.GetProperty("entries").EnumerateArray())
        {
            var id=item.GetProperty("id").GetString()!;
            if(id.Length>64 || id.Any(c=>!char.IsAsciiLetterOrDigit(c) && c!='-')) throw new InvalidDataException("Invalid view ID");
            long size=item.GetProperty("size").GetInt64(),offset=item.GetProperty("map_offset").GetInt64();
            int compressedLength=item.GetProperty("map_compressed_bytes").GetInt32(),count=item.GetProperty("extent_count").GetInt32();
            if(size<=0 || count is < 1 or > 3000000 || compressedLength is < 1 or > 64000000 || offset<8 || offset>indexOffset-compressedLength
               || entries.Count>=32 || totalExtents>4000000-count) throw new InvalidDataException("Invalid view map");
            totalExtents+=count;
            file.Position=offset;var packed=new byte[compressedLength];file.ReadExactly(packed);
            using var zlib=new ZLibStream(new MemoryStream(packed),CompressionMode.Decompress);
            var map=new byte[checked(count*24)];zlib.ReadExactly(map);
            if(zlib.ReadByte()!=-1) throw new InvalidDataException("Oversized view map");
            var extents=new Extent[count];long expected=0;
            for(int i=0;i<count;i++)
            {
                var bytes=map.AsSpan(i*24,24);
                long logical=checked((long)BinaryPrimitives.ReadUInt64LittleEndian(bytes));
                long physical=checked((long)BinaryPrimitives.ReadUInt64LittleEndian(bytes[8..]));
                long length=checked((long)BinaryPrimitives.ReadUInt64LittleEndian(bytes[16..]));
                if(logical!=expected || length<=0 || length>size-expected || physical<8 || physical>offset-length) throw new InvalidDataException("Invalid archive extent");
                extents[i]=new Extent(logical,physical,length);expected+=length;
            }
            if(expected!=size) throw new InvalidDataException("Incomplete archive view");
            var sha=item.GetProperty("sha256").GetString()!;
            if(sha.Length!=64 || !sha.All(char.IsAsciiHexDigit)) throw new InvalidDataException("Invalid media checksum");
            long duration=item.GetProperty("duration_ticks").GetInt64();
            if(duration<=0 || duration>TimeSpan.FromDays(2).Ticks) throw new InvalidDataException("Invalid media duration");
            entries.Add(id,new ArchiveEntry(id,item.GetProperty("label").GetString()!,size,sha,duration,extents));
        }
        Entries=entries;
    }
}

public sealed class ArchiveViewStream : Stream
{
    private readonly FileStream _file;
    private readonly ArchiveEntry _entry;
    private long _position;
    public ArchiveViewStream(string path,ArchiveEntry entry)
    {
        _file=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read,65536,FileOptions.RandomAccess|FileOptions.Asynchronous);
        _entry=entry;
    }
    public override bool CanRead=>true;
    public override bool CanSeek=>true;
    public override bool CanWrite=>false;
    public override long Length=>_entry.Size;
    public override long Position { get=>_position;set=>Seek(value,SeekOrigin.Begin); }
    private Extent Current()
    {
        int low=0,high=_entry.Extents.Length-1;
        while(low<=high)
        {
            int middle=low+(high-low)/2;var e=_entry.Extents[middle];
            if(_position<e.Logical) high=middle-1;
            else if(_position>=e.Logical+e.Length) low=middle+1;
            else return e;
        }
        throw new InvalidDataException("Missing view extent");
    }
    public override int Read(byte[] buffer,int offset,int count)=>Read(buffer.AsSpan(offset,count));
    public override int Read(Span<byte> buffer)
    {
        if(_position>=Length || buffer.Length==0) return 0;
        var e=Current();int count=(int)Math.Min(buffer.Length,e.Logical+e.Length-_position);
        _file.Position=e.Physical+_position-e.Logical;
        int read=_file.Read(buffer[..count]);if(read==0) throw new EndOfStreamException("Shared archive truncated");
        _position+=read;return read;
    }
    public override async ValueTask<int> ReadAsync(Memory<byte> buffer,CancellationToken cancellationToken=default)
    {
        if(_position>=Length || buffer.Length==0) return 0;
        var e=Current();int count=(int)Math.Min(buffer.Length,e.Logical+e.Length-_position);
        _file.Position=e.Physical+_position-e.Logical;
        int read=await _file.ReadAsync(buffer[..count],cancellationToken);
        if(read==0) throw new EndOfStreamException("Shared archive truncated");
        _position+=read;return read;
    }
    public override long Seek(long offset,SeekOrigin origin)
    {
        long next=origin switch { SeekOrigin.Begin=>offset,SeekOrigin.Current=>checked(_position+offset),SeekOrigin.End=>checked(Length+offset),_=>throw new ArgumentException("Invalid seek origin") };
        if(next<0 || next>Length) throw new IOException("Seek outside media view");
        return _position=next;
    }
    protected override void Dispose(bool disposing){if(disposing)_file.Dispose();base.Dispose(disposing);}
    public override async ValueTask DisposeAsync(){await _file.DisposeAsync();GC.SuppressFinalize(this);}
    public override void Flush() { }
    public override void SetLength(long value)=>throw new NotSupportedException();
    public override void Write(byte[] buffer,int offset,int count)=>throw new NotSupportedException();
}

[ApiController]
[AllowAnonymous]
[Route("SharedEditions")]
public sealed class ArchiveController : ControllerBase
{
    [HttpGet("{cut}/source.mkv")]
    [HttpHead("{cut}/source.mkv")]
    [HttpGet("{archiveId}/{cut}/source.mkv")]
    [HttpHead("{archiveId}/{cut}/source.mkv")]
    public IActionResult Source(string cut,string? archiveId=null)
    {
        // Stable service-only locator. Each actual media read receives a fresh expiring ticket.
        // The locator capability is bound to the exact cut checksum and never grants public access.
        if(HttpContext.Connection.RemoteIpAddress is not { } ip || !IPAddress.IsLoopback(ip)) return NotFound();
        var archive=ArchiveCatalog.Get(archiveId);
        var secret=ArchiveCatalog.Secret;
        if(archive is null || !archive.Entries.TryGetValue(cut,out var entry) || string.IsNullOrEmpty(secret)
           || Request.Query["access"].Count!=1 || Request.Query["access"].ToString().Length!=64) return NotFound();
        try
        {
            var expected=HMACSHA256.HashData(Encoding.UTF8.GetBytes(secret),Encoding.UTF8.GetBytes(ArchiveCatalog.CapabilityMessage(archiveId,cut,entry.Sha256)));
            if(!CryptographicOperations.FixedTimeEquals(expected,Convert.FromHexString(Request.Query["access"].ToString()))) return NotFound();
        }
        catch(FormatException){return NotFound();}
        var route=archiveId is null ? $"/SharedEditions/{cut}/movie.mkv" : $"/SharedEditions/{archiveId}/{cut}/movie.mkv";
        long expires=DateTimeOffset.UtcNow.AddHours(4).ToUnixTimeSeconds();
        var signature=Convert.ToHexString(HMACSHA256.HashData(Encoding.UTF8.GetBytes(secret),Encoding.UTF8.GetBytes($"{route}|{expires}"))).ToLowerInvariant();
        return Redirect($"{route}?expires={expires}&sig={signature}");
    }

    [HttpGet("{cut}/movie.mkv")]
    [HttpHead("{cut}/movie.mkv")]
    [HttpGet("{archiveId}/{cut}/movie.mkv")]
    [HttpHead("{archiveId}/{cut}/movie.mkv")]
    public IActionResult Movie(string cut,string? archiveId=null)
    {
        // Internal media-source service only. The client uses Jellyfin's ordinary authenticated
        // video endpoint, enforced by PrivateSourceTransport, never this loopback source URL.
        if(HttpContext.Connection.RemoteIpAddress is not { } ip || !IPAddress.IsLoopback(ip)) return NotFound();
        var secret=ArchiveCatalog.Secret;
        if(string.IsNullOrEmpty(secret) || Request.Query["expires"].Count!=1 || Request.Query["sig"].Count!=1
           || !long.TryParse(Request.Query["expires"],out var expires)) return NotFound();
        long now=DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        if(expires<now || expires>now+14400 || Request.Query["sig"].ToString().Length!=64) return NotFound();
        try
        {
            var expected=HMACSHA256.HashData(Encoding.UTF8.GetBytes(secret),Encoding.UTF8.GetBytes($"{Request.Path}|{expires}"));
            if(!CryptographicOperations.FixedTimeEquals(expected,Convert.FromHexString(Request.Query["sig"].ToString()))) return NotFound();
        }
        catch(FormatException){return NotFound();}
        var archive=ArchiveCatalog.Get(archiveId);
        if(archive is null || !archive.Entries.TryGetValue(cut,out var entry)) return NotFound();
        return new FileStreamResult(new ArchiveViewStream(archive.Path,entry),"video/x-matroska")
        { EnableRangeProcessing=true,EntityTag=new EntityTagHeaderValue('"'+entry.Sha256+'"') };
    }
}
