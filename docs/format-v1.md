# MKVA format v1

The filename extension is `.mkva`; the binary signature remains `JFSHARE1` for
compatibility with the earlier `.jfshare` prototype. An extension rename does not
alter the file layout or turn the archive into a standard Matroska container.

1. Eight-byte ASCII `JFSHARE1` header.
2. Stored original-media byte objects and compressed edition maps, interleaved as
   the builder completes editions.
3. UTF-8 JSON index with `format: 1` and `entries`.
4. A 24-byte little-endian footer: uint64 index offset, uint64 index length,
   eight-byte `JFSHARE1` signature.

Each entry records its `id`, `label`, original `size`, original full-file `sha256`,
`duration_ticks`, `map_offset`, `map_compressed_bytes` and `extent_count`. Its map
is zlib-compressed. Each decompressed record is three little-endian uint64 values:
logical original-file offset, physical archive offset, length.

Ordered extents cover every byte of an original file without gaps. Shared extents
may refer backwards to payload data already stored for another edition. Metadata
and non-shared original bytes remain in the archive too; this is not just a delta
requiring the source disc or MKVs to remain available.

The builder shares exact chunks of encoded Block/SimpleBlock payload data. It does
not identify scene boundaries, concatenate editions, decode media, or re-encode it.
It verifies every full reconstructed MKV SHA-256 before publishing the local file.
Recovery independently verifies the original checksum and refuses existing outputs.

The server currently bounds the JSON index to 10,000,000 bytes, each map to
3,000,000 extents / 64,000,000 compressed bytes, the catalogue entry count to 32,
and the combined extent count to 4,000,000. The integration must enforce installed
reader limits before copying large archives. This is a versioned prototype layout,
not a Matroska standard or a general archival integrity/redundancy system.

## Optional sidecar attachments

New archives may include an `attachments` array beside `entries`. Each attachment
uses the same extent-map fields and checksum, plus its original `name` and optional
`owner` edition ID. A null owner denotes a common file. NFOs, artwork and external
subtitle files are stored as original bytes. They are not movie editions and must
not be registered as playback entries.

Existing Jellyfin readers iterate only `entries`; the optional catalogue requires
no change to their binary reader. Recovery tools can restore attachments as well
as the selected original MKVs. Media entries retain required positive
`duration_ticks`, while sidecar attachments do not have a duration.
