using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Entities.Movies;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Plugins;
using MediaBrowser.Controller.Providers;
using MediaBrowser.Model.Dto;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Filters;
using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Authorization;
using Microsoft.Extensions.DependencyInjection;
using Jellyfin.Database.Implementations.Enums;
using Jellyfin.Data;

namespace SharedEditions;

public sealed class Configuration : BasePluginConfiguration
{
    public string MoviesRoot { get; set; }="/data/Media/Films";
    public string SigningKey { get; set; }="";
    public List<ArchiveRegistration> Archives { get; set; }=[];
}
public sealed class Plugin : BasePlugin<Configuration>
{
    public static Plugin? Instance { get; private set; }
    public Plugin(IApplicationPaths paths,IXmlSerializer serializer):base(paths,serializer) { Instance=this; }
    public override string Name=>"MKV Archive";
    public override Guid Id=>Guid.Parse("9fc320f8-4118-4bdc-9261-7e421f2954d0");
    public override string Description=>"MKVA archives with shared storage and byte-exact MKV editions, served through Jellyfin's normal authenticated playback.";
}

internal static class Sources
{
    internal static ArchiveEntry? Match(string? path)
    {
        if(!Uri.TryCreate(path,UriKind.Absolute,out var uri) || !uri.IsLoopback) return null;
        var parts=uri.AbsolutePath.Split('/');
        if(parts.Length is not (4 or 5) || parts[1]!="SharedEditions" || parts[^1] is not ("source.mkv" or "movie.mkv")) return null;
        return ArchiveCatalog.Describe(parts.Length==5 ? parts[2] : null,parts[^2]);
    }
}

public sealed class Services : IPluginServiceRegistrator
{
    public void RegisterServices(IServiceCollection services,IServerApplicationHost host)
    {
        services.AddScoped<InternalSourceTransport>();
        services.AddScoped<SharedMediaAuthorization>();
        services.Configure<MvcOptions>(options=>options.Filters.AddService<InternalSourceTransport>());
        services.Configure<MvcOptions>(options=>options.Filters.AddService<SharedMediaAuthorization>());
    }
}

// Jellyfin's static remote-media route does not carry [Authorize]. Require the
// server's own default authentication/authorization policy for our movies only.
public sealed class SharedMediaAuthorization(ILibraryManager library,IUserManager users,IAuthorizationService authorization,IAuthorizationPolicyProvider policies) : IAsyncAuthorizationFilter
{
    public async Task OnAuthorizationAsync(AuthorizationFilterContext context)
    {
        var request=context.HttpContext.Request;
        if(!request.Path.StartsWithSegments("/Videos",StringComparison.OrdinalIgnoreCase)) return;
        var ids=request.Query["MediaSourceId"].Select(value=>value ?? "").ToList();
        if(context.RouteData.Values.TryGetValue("itemId",out var route)) ids.Add(route?.ToString() ?? "");
        var protectedItems=new List<BaseItem>();
        foreach(var text in ids)
        {
            if(!Guid.TryParse(text,out var id)) continue;
            var item=library.GetItemById<BaseItem>(id);
            if(item?.Path is not { } path || System.IO.Path.GetExtension(path)!=".strm"
               || !File.Exists(path) || new FileInfo(path).Length>4096) continue;
            if(Sources.Match(File.ReadAllText(path).Trim()) is not null) protectedItems.Add(item);
        }
        if(protectedItems.Count==0) return;
        var auth=await context.HttpContext.AuthenticateAsync();
        if(!auth.Succeeded || auth.Principal is null)
        {
            context.Result=new UnauthorizedResult();return;
        }
        context.HttpContext.User=auth.Principal;
        var policy=await policies.GetDefaultPolicyAsync();
        if(!(await authorization.AuthorizeAsync(auth.Principal,context.HttpContext,policy)).Succeeded)
            context.Result=new ForbidResult();
        if(context.Result is not null) return;
        if(auth.Principal.IsInRole("Administrator") && auth.Principal.FindFirst("Jellyfin-IsApiKey")?.Value=="True") return;
        var user=Guid.TryParse(auth.Principal.FindFirst("Jellyfin-UserId")?.Value,out var userId) && userId!=Guid.Empty ? users.GetUserById(userId) : null;
        if(user is null || !user.HasPermission(PermissionKind.EnableMediaPlayback)
            || protectedItems.Any(item=>!item.IsVisibleStandalone(user))) context.Result=new ForbidResult();
    }
}

// Do not alter codec negotiation or the caller's permissions. Mark only our private service
// URLs as server-managed so clients use /Videos/.../stream instead of opening loopback themselves.
public sealed class InternalSourceTransport : IAsyncResultFilter
{
    private static void Normalize(object value)
    {
        if(value.GetType().GetProperty("MediaSources")?.GetValue(value) is IEnumerable<MediaSourceInfo> sources)
            foreach(var source in sources)
                if(Sources.Match(source.Path) is { } entry)
                {
                    source.IsRemote=false;
                    source.Name=entry.Label;
                    // The .strm is only a locator. Report the reconstructed edition's
                    // media size and duration, never the tiny pointer's file size.
                    source.Size=entry.Size;
                    source.RunTimeTicks=entry.DurationTicks;
                }

        // TV clients can obtain details through an Items query rather than the
        // single-item route. Apply the same source metadata to both response shapes.
        if(value is IEnumerable<BaseItemDto> items)
            foreach(var item in items) Normalize(item);
        else if(value.GetType().GetProperty("Items")?.GetValue(value) is IEnumerable<BaseItemDto> results)
            foreach(var item in results) Normalize(item);
    }

    public async Task OnResultExecutionAsync(ResultExecutingContext context,ResultExecutionDelegate next)
    {
        if(context.Result is ObjectResult { Value: { } value }) Normalize(value);
        await next();
    }
}

public sealed class TimelineMetadata : ICustomMetadataProvider<Movie>
{
    public string Name=>"Shared Editions exact duration";
    public Task<ItemUpdateType> FetchAsync(Movie item,MetadataRefreshOptions options,CancellationToken cancellationToken)
    {
        if(string.IsNullOrEmpty(item.Path) || Path.GetExtension(item.Path)!=".strm" || !File.Exists(item.Path)
           || new FileInfo(item.Path).Length>4096) return Task.FromResult(ItemUpdateType.None);
        var entry=Sources.Match(File.ReadAllText(item.Path).Trim());
        if(entry is null) return Task.FromResult(ItemUpdateType.None);
        item.RunTimeTicks=entry.DurationTicks;
        return Task.FromResult(ItemUpdateType.MetadataImport);
    }
}
