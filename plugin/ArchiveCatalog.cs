using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using MediaBrowser.Common.Api;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace SharedEditions;

public sealed class RegisteredEdition
{
    public string Id { get; set; }="";
    public string Label { get; set; }="";
    public string Sha256 { get; set; }="";
    public long Size { get; set; }
    public long DurationTicks { get; set; }
}
public sealed class ArchiveRegistration
{
    public string Id { get; set; }="";
    public string Path { get; set; }="";
    public string Sha256 { get; set; }="";
    public long Size { get; set; }
    public List<RegisteredEdition> Entries { get; set; }=[];
}

internal static class ArchiveCatalog
{
    internal static readonly object Sync=new();
    private static readonly Dictionary<string,(SharedArchive Archive,long Stamp)> Cache=new();
    internal static string? Secret=>Environment.GetEnvironmentVariable("SHARED_EDITIONS_SIGNING_KEY") ?? Plugin.Instance?.Configuration.SigningKey;
    internal static string CapabilityMessage(string? id,string cut,string sha)=>id is null ? $"source|{cut}|{sha}" : $"source|{id}|{cut}|{sha}";
    internal static ArchiveEntry? Describe(string? id,string cut)
    {
        if(id is null) return SharedArchive.Configured?.Entries.GetValueOrDefault(cut);
        lock(Sync)
        {
            var edition=Plugin.Instance?.Configuration.Archives.SingleOrDefault(x=>x.Id==id)?.Entries.SingleOrDefault(x=>x.Id==cut);
            // Lists/details only need metadata. Do not load every film's potentially
            // large extent maps just because a client browses a page of movies.
            return edition is null ? null : new ArchiveEntry(edition.Id,edition.Label,edition.Size,edition.Sha256,edition.DurationTicks,[]);
        }
    }
    internal static SharedArchive? Get(string? id)
    {
        if(id is null) return SharedArchive.Configured;
        if(!Regex.IsMatch(id,"^[a-f0-9]{32}$")) return null;
        lock(Sync)
        {
            var record=Plugin.Instance?.Configuration.Archives.SingleOrDefault(x=>x.Id==id);
            if(record is null || !File.Exists(record.Path)) return null;
            var file=new FileInfo(record.Path);
            if(file.Length!=record.Size) throw new InvalidDataException("Registered archive size changed");
            if(Cache.TryGetValue(id,out var cached))
            {
                if(cached.Stamp!=file.LastWriteTimeUtc.Ticks) throw new InvalidDataException("Registered archive changed; originals retained");
                return cached.Archive;
            }
            var archive=record.Path==Environment.GetEnvironmentVariable("SHARED_EDITIONS_ARCHIVE")
                ? SharedArchive.Configured! : new SharedArchive(record.Path);
            if(archive.Entries.Count!=record.Entries.Count || record.Entries.Any(x=>!archive.Entries.TryGetValue(x.Id,out var e)
                || e.Sha256!=x.Sha256 || e.Size!=x.Size || e.Label!=x.Label || e.DurationTicks!=x.DurationTicks))
                throw new InvalidDataException("Archive editions differ from registration");
            // Bound idle catalogue RAM on the 1 GB server. Existing playback streams
            // retain their own entry/map references when an idle archive is evicted.
            Cache.Clear();
            Cache.Add(id,(archive,file.LastWriteTimeUtc.Ticks));
            return archive;
        }
    }
}

// Registration is an administrator-only preparation step. An absent destination
// stays unreadable until the separately verified NAS copy has been published.
[ApiController]
[Authorize(Policy=Policies.RequiresElevation)]
[Route("MKVArchive")]
public sealed class CatalogController : ControllerBase
{
    [HttpPost("Register")]
    public IActionResult Register([FromBody] ArchiveRegistration request)
    {
        var plugin=Plugin.Instance!;
        var root=Path.GetFullPath(plugin.Configuration.MoviesRoot).TrimEnd(Path.DirectorySeparatorChar)+Path.DirectorySeparatorChar;
        var path=Path.GetFullPath(request.Path);
        if(!path.StartsWith(root,StringComparison.Ordinal) || !path.EndsWith(".mkva",StringComparison.OrdinalIgnoreCase)
            || request.Size<32 || !Regex.IsMatch(request.Sha256,"^[a-f0-9]{64}$")
            || request.Entries.Count is <2 or >32 || request.Entries.Select(x=>x.Id).Distinct().Count()!=request.Entries.Count
            || request.Entries.Any(x=>!Regex.IsMatch(x.Id,"^[a-zA-Z0-9-]{1,64}$") || !Regex.IsMatch(x.Sha256,"^[a-f0-9]{64}$")
                || x.Size<=0 || x.DurationTicks<=0 || x.DurationTicks>TimeSpan.FromDays(2).Ticks || string.IsNullOrWhiteSpace(x.Label) || x.Label.Length>120))
            return BadRequest("Invalid immutable archive registration");
        for(var cursor=new DirectoryInfo(Path.GetDirectoryName(path)!);cursor is not null && cursor.FullName.StartsWith(root.TrimEnd('/'),StringComparison.Ordinal);cursor=cursor.Parent)
            if(cursor.Exists && cursor.LinkTarget is not null) return BadRequest("Archive links are unsupported");
        if(System.IO.File.Exists(path) && new FileInfo(path).LinkTarget is not null) return BadRequest("Archive links are unsupported");
        request.Id=request.Sha256[..32];request.Path=path;
        lock(ArchiveCatalog.Sync)
        {
            var existing=plugin.Configuration.Archives.SingleOrDefault(x=>x.Id==request.Id || x.Path==path);
            if(existing is not null && (existing.Id!=request.Id || existing.Path!=path || existing.Sha256!=request.Sha256 || existing.Size!=request.Size
                || existing.Entries.Count!=request.Entries.Count || existing.Entries.Zip(request.Entries).Any(pair=>pair.First.Id!=pair.Second.Id || pair.First.Label!=pair.Second.Label || pair.First.Sha256!=pair.Second.Sha256 || pair.First.Size!=pair.Second.Size || pair.First.DurationTicks!=pair.Second.DurationTicks)))
                return Conflict("Registered path/content is immutable");
            if(existing is null) plugin.Configuration.Archives.Add(request);
            if(string.IsNullOrEmpty(ArchiveCatalog.Secret)) plugin.Configuration.SigningKey=Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
            plugin.SaveConfiguration();
        }
        var secret=ArchiveCatalog.Secret!;
        // Keep existing legacy pointers valid when adopting the first archive.
        bool legacy=Environment.GetEnvironmentVariable("SHARED_EDITIONS_ARCHIVE")==path;
        var entries=request.Entries.Select(e=>
        {
            string? id=legacy ? null : request.Id;
            string capability=Convert.ToHexString(HMACSHA256.HashData(Encoding.UTF8.GetBytes(secret),Encoding.UTF8.GetBytes(ArchiveCatalog.CapabilityMessage(id,e.Id,e.Sha256)))).ToLowerInvariant();
            string route=legacy ? $"/SharedEditions/{e.Id}/source.mkv" : $"/SharedEditions/{request.Id}/{e.Id}/source.mkv";
            return new { e.Id,Url=$"http://127.0.0.1:8096{route}?access={capability}" };
        }).ToArray();
        return Ok(new { request.Id,Entries=entries });
    }

    [HttpGet("Verify/{id}")]
    public IActionResult Verify(string id)
    {
        var archive=ArchiveCatalog.Get(id);
        if(archive is null) return NotFound();
        var entries=archive.Entries.Values.Select(e=>
        {
            using var stream=new ArchiveViewStream(archive.Path,e);
            var samples=new List<object>();
            foreach(long position in new[]{0,e.Size/2,Math.Max(0,e.Size-4096)}.Distinct())
            {
                stream.Position=position;var bytes=new byte[Math.Min(4096,e.Size-position)];stream.ReadExactly(bytes);
                samples.Add(new { Position=position,Sha256=Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant() });
            }
            return new { e.Id,e.Sha256,e.Size,Samples=samples };
        }).ToArray();
        return Ok(new { Id=id,Entries=entries });
    }
}
