# MKVA — MKV Archive

Lossless shared storage for multiple editions of one film, with a Jellyfin plugin
that presents each edition as its original, seekable MKV.

**MKVA is a custom archive format, not an ordinary MKV or an official Matroska
extension.** Normal media players cannot open the archive directly. The plugin
serves exact MKV views, and the independent Python tool can recover standalone MKVs.

## What it does

- Stores identical compressed Matroska Block/SimpleBlock payload chunks once.
- Preserves each original file's headers, timestamps, tracks, subtitles, chapters,
  cues, attachments and other bytes through a per-edition extent map.
- Verifies every complete reconstructed MKV checksum before publishing an archive.
- Serves byte ranges through stock Jellyfin and FFmpeg without re-encoding.
- Keeps different films isolated with immutable archive IDs and separate routes.
- Reports edition labels, sizes, durations and stream information through normal
  Jellyfin media sources; audio/subtitles remain client-selectable.

The current plugin version is **0.3.1**, targeting **Jellyfin 12.3 / .NET 10**.
This remains a prototype. Codec negotiation/transcoding still belongs to Jellyfin.
Client compatibility beyond the validated cases is not guaranteed.

## Repository

- `plugin/`: C# Jellyfin plugin and installation metadata.
- `tools/mkva_archive.py`: standalone standard-library Python packer and recovery tool.
- `docs/format-v1.md`: archive layout and compatibility notes.
- `docs/plugin.md`: installation, configuration and integration protocol.
- `tests/`: portable byte-exact reconstruction and recovery tests.

No media, credentials, runtime configuration, local deployment scripts, user
databases or server reference assemblies are included. Dr Rip source is not
included; an external application can use the documented registration protocol.

## Pack and recover

Create a JSON array describing the already-approved original MKVs:

```json
[
  {"id":"theatrical","label":"Theatrical","path":"/staging/theatrical.mkv","duration_ticks":54000000000},
  {"id":"extended","label":"Extended Edition","path":"/staging/extended.mkv","duration_ticks":60000000000}
]
```

`duration_ticks` uses 100-nanosecond units. Supply `expected_sha256` per entry when
binding inputs to independently approved files. Edition IDs must use ASCII letters,
digits or hyphens and remain unique within the archive.

```sh
python -I -B tools/mkva_archive.py editions.json movie.mkva
python -I -B tools/mkva_archive.py extract movie.mkva theatrical recovered.mkv
python -I -B -m unittest discover -s tests -v
```

The tool refuses existing archive/recovery output paths. Interrupted partial files
remain for explicit review. Retain originals and an independent backup until full
verification and actual-client playback have been accepted. The standalone tools
do **not** delete originals, transfer to a library or perform TMM metadata scraping.

## Build the plugin

Install a .NET 10 SDK and obtain matching Jellyfin 12.3 server assemblies. Point the
build at their directory; these dependencies are intentionally not redistributed:

```sh
dotnet build plugin/SharedEditions.csproj -c Release -p:JellyfinServerDirectory=/path/to/jellyfin/assemblies
```

Install only the plugin DLL and `meta.json`, following [the plugin guide](docs/plugin.md).
The archive is not automatically discovered merely by copying it into a movie
library: it also needs registration, metadata and the edition STRM pointers.

## Limits

Packing is intended for a workstation, not a small Jellyfin server. Payload hash
indexes and extent maps consume RAM. The builder has no active cancellation or
partial-build resume. The plugin validates format bounds and keeps a bounded idle
map cache; simultaneous active streams still retain their required maps.

Savings depend on identical compressed payload bytes. Independent re-encodes may
share little. Keep weak/no-saving results out of automatic publication workflows.
The plugin supports its registered catalogue and the legacy single-archive
environment configuration; it does not provide an admin configuration screen yet.

The stable plugin GUID, assembly name and `JFSHARE1` archive signature intentionally
retain the project's earlier Shared Editions identity for compatibility. Legacy
`.jfshare` files use the same v1 layout and remain readable.
