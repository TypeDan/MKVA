# Jellyfin plugin and integration

## Install

Target: Jellyfin 12.3 / .NET 10. Back up the existing plugin/configuration first.
Put `Jellyfin.Plugin.SharedEditions.dll` and `meta.json` in one plugin folder such
as `MKV Archive_0.3.1.0`, then restart Jellyfin only when no viewer is playing.
Do not install multiple copies of the same plugin GUID/assembly. No Jellyfin binary
patch, custom FFmpeg, FUSE mount or extra daemon is required.

The plugin GUID is `9fc320f8-4118-4bdc-9261-7e421f2954d0`. Its existing assembly
name is retained deliberately so upgrades keep the same plugin identity.

## Configuration

Normal plugin configuration XML contains `MoviesRoot`, `SigningKey` and `Archives`.
`MoviesRoot` defaults to `/data/Media/Films`; set it to the actual server-side movie
directory before registering archives elsewhere. Protect configuration files and
never expose the signing key in logs, client settings or source control. A key is
generated on registration if no legacy environment key/configured key exists.

Legacy single-archive deployments can continue to use `SHARED_EDITIONS_ARCHIVE`
and `SHARED_EDITIONS_SIGNING_KEY`. Preserve their signing key and locators during
migration. The `SHARED_EDITIONS_ARCHIVE` value must follow a deliberately moved file.
This version does not supply an admin configuration page or turnkey installer.

## Register a prepared publication

An authenticated administrator calls `POST /MKVArchive/Register`. Use the server's
normal Authorization header; do not put API keys into query strings or browser UI.
Supply the final server-side `.mkva` path, full archive SHA-256, archive byte size
and the exact edition index metadata:

```json
{
  "Path":"/data/Media/Films/Example Movie (2000)/Example Movie (2000).mkva",
  "Sha256":"FULL_ARCHIVE_SHA256",
  "Size":123456,
  "Entries":[
    {"Id":"theatrical","Label":"Theatrical","Sha256":"ORIGINAL_MKV_SHA256","Size":234567,"DurationTicks":54000000000},
    {"Id":"extended","Label":"Extended Edition","Sha256":"OTHER_ORIGINAL_MKV_SHA256","Size":345678,"DurationTicks":60000000000}
  ]
}
```

Replace all example values with real independently verified data. The registration
needs two to 32 editions and only accepts archive paths under `MoviesRoot`.
It returns an archive `Id` and an `Entries` array of edition IDs and signed private
loopback `Url` locators. Never print or publish those capability URLs.

Registration may precede the final NAS copy. It is preparation only: a missing
archive cannot be played, and registration does not authorize deleting sources.
Registrations bind an immutable path/content identity. A conflicting retry fails;
an identical retry returns the same registration. Changing a registered archive
in place is unsupported. Source routes for new registrations include both archive
and edition IDs, preventing cut-ID collisions between films.

Write one UTF-8 STRM file per returned locator using normal multiple-version names:
`Example Movie (2000) - Theatrical.strm` and
`Example Movie (2000) - Extended Edition.strm`. Keep corresponding per-edition
NFOs and artwork from the original MKVs' metadata workflow alongside the archive.
Use one movie folder, refresh the intended library, and verify its identity and
edition media sources. Do not scrape the custom archive as though it were an MKV.

## Verify before retiring any source

The publishing application must first verify complete original-view checksums and
then checksum the actual NAS archive and every transferred sidecar. Registration
does not perform that full NAS hash: keep heavy archive work on the workstation.

An administrator can call `GET /MKVArchive/Verify/{archiveId}`. The response returns
each edition ID, original SHA-256/size and `Samples` containing byte `Position` and
`Sha256` for 4096-byte windows at the beginning, middle and end (shorter for tiny
files). Compare these against the original MKVs. This confirms the server sees
and reconstructs the intended copy; samples alone do not prove complete integrity.

Also verify one movie entry with all intended edition routes and actual-client
playback. Only the owning application/user may then apply its explicit, scoped
cleanup policy. The plugin itself never deletes original media or staging files.

## Playback/security

Private source endpoints accept loopback requests with signed capabilities; stable
locators issue expiring read tickets. A filter keeps those sources server-managed
in detail and list responses, so clients use Jellyfin's normal video route rather
than opening localhost themselves. That route requires authentication and checks
the user's playback permission and item visibility. Catalogue endpoints require
administrator privileges. Codec/container negotiation remains Jellyfin's job.

Stored archive metadata can be returned without loading every film's extent maps.
The reader validates map bounds and caches one idle registered archive; active
streams retain their own maps. Size/mtime changes to a cached immutable archive
are rejected. This is not a sustained concurrency benchmark or a full security audit.
