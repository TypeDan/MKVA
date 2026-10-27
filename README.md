# MKVA (MKV Archive)

MKVA stores multiple editions of a film in one archive, sharing identical encoded
media payloads to save space. Each edition can be reconstructed byte for byte.
The Jellyfin plugin exposes those editions as seekable MKV streams.

MKVA is a custom format. It is not a standard MKV container or a Matroska extension,
and media players cannot open it directly. Use the plugin for playback or the
Python tool to recover a standalone MKV.

## Status

Version 0.3.1 is a prototype targeting Jellyfin 12.3 and .NET 10. Check it against
your server and clients before relying on it. Keep the original files and an
independent backup until you have verified recovery and playback.

Space savings depend on identical compressed bytes. Two independent encodes of
the same film may share little data. Headers, timestamps, tracks, subtitles,
chapters, cues and attachments are preserved in each edition's byte map.

## Pack and recover

The Python tool uses only the standard library. Create an `editions.json` file:

```json
[
  {"id":"theatrical","label":"Theatrical","path":"/staging/theatrical.mkv","duration_ticks":54000000000},
  {"id":"extended","label":"Extended Edition","path":"/staging/extended.mkv","duration_ticks":60000000000}
]
```

Durations are in 100-nanosecond ticks. Use unique edition IDs containing ASCII
letters, digits or hyphens. Add `expected_sha256` to each entry to check the input
against a checksum you recorded independently.

```sh
python -I -B tools/mkva_archive.py editions.json movie.mkva
python -I -B tools/mkva_archive.py extract movie.mkva theatrical recovered.mkv
```

Packing verifies the full checksum of every reconstructed edition before renaming
the completed archive. Recovery also checks the original checksum. Existing output
files are refused; interrupted builds leave a `.partial` file for inspection.
Run Python without `-O`: the tool uses assertions for validation.

The tool does not delete originals, move files into a library or fetch metadata.
Archives retain original filenames and supplied edition metadata; verification
receipts contain local output paths. Review these before sharing your own archives
or receipts.

## Jellyfin plugin

Build with a .NET 10 SDK and matching Jellyfin 12.3 server assemblies:

```sh
dotnet build plugin/SharedEditions.csproj -c Release -p:JellyfinServerDirectory=/path/to/jellyfin/assemblies
```

Server assemblies are not included. Install the plugin DLL and `meta.json` using
[the plugin guide](docs/plugin.md). The guide covers configuration, archive
registration, STRM pointers and playback verification. Copying an archive into a
movie folder alone does not make it playable.

The plugin provides edition labels, sizes and durations through Jellyfin media
sources. Jellyfin handles codec negotiation and transcoding. Client compatibility
needs testing on the clients you use.

## Development

- `plugin/`: C# plugin and installation metadata.
- `tools/mkva_archive.py`: packer and recovery tool.
- [Format v1](docs/format-v1.md): archive layout and reader limits.
- [Plugin guide](docs/plugin.md): installation and integration API.
- `tests/`: synthetic EBML fixtures for reconstruction and recovery.

Run the archive tests:

```sh
python -I -B -m unittest discover -s tests -v
```

These tests check byte layout and recovery, not real media playback.

Packing keeps payload hashes and extent maps in RAM and is intended for a
workstation. Builds cannot yet be resumed. The plugin bounds its idle map cache,
but active streams retain their maps. There is no admin configuration screen.

The assembly name, plugin GUID and `JFSHARE1` signature retain the earlier Shared
Editions identity for compatibility. Legacy `.jfshare` archives use the same v1
layout.
