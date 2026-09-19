### Title
Heap out-of-bounds read in ZIP central-directory extra-field parsing when consuming an untrusted, hash-pinned jar/archive - ([File: third_party/ijar/zip.cc])

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` walks the "extra fields" block of a ZIP central directory record without validating that each record's declared length stays inside the block, allowing a crafted archive to make the parser read past the mapped extra-field/central-directory region.

### Finding Description
`ProcessCentralDirEntry` reads `file_name_length`, `extra_field_length`, and other attacker-controlled 16/32-bit fields directly from the mmap'd input file with no independent bounds re-validation against the file size for the extra-field loop: [1](#0-0) 

It then iterates the extra-field bytes with: [2](#0-1) 

The loop condition is `extra_p != p`, where `p = start + extra_field_length`. Each iteration reads a 2-byte `header_id` and 2-byte `data_size` (4 bytes total) and then advances `extra_p += data_size`. There is no check that:
1. At least 4 bytes remain between `extra_p` and `p` before calling `get_u2le` twice, and
2. `extra_p + data_size <= p` after advancing.

If an attacker crafts a central-directory extra-field block whose last record's `data_size` does not land `extra_p` exactly on `p` (e.g., an odd total length, or a `data_size` value chosen so the cursor overshoots), `extra_p` will skip past `p` and the `while (extra_p != p)` condition remains true indefinitely (pointers "crossing" without becoming equal). Parsing then continues into adjacent central-directory bytes (file comment, next entry's header) and, because `data_size` is a fully attacker-controlled 16-bit value (0–65535) read from those bytes, can push `extra_p` far beyond the mapped file region on a subsequent iteration — this is `mmap`-backed memory (`MappedInputFile`), so walking past the mapping's end reads unmapped or adjacent heap/heap-guard memory: a classic heap-based out-of-bounds read (CWE-125), directly analogous to the pdfalto CVE-2018-18274 bug class (unchecked field-length arithmetic in an untrusted-file parser).

This code path (`InputZipFile`/`ZipExtractor`) is exercised whenever Bazel or its build tooling parses a ZIP/JAR whose bytes originate from an untrusted, externally-hosted artifact after checksum verification — e.g., a jar fetched via `http_jar`/`http_archive` (checksum-pinned) that is later processed by the native `ijar` tool (invoked by Java rules such as `java_common.run_ijar` on every external Java dependency) to produce an interface jar, or by `PartialZipExtractor`/`ZipExtractor` in `src/main/cpp/archive_utils.cc`. The declared `sha256`/`integrity` on the download only binds to the exact bytes served; it does not (and cannot) validate the internal ZIP structural invariants that this parser assumes, so a malicious publisher can serve content that passes the pinned hash yet is crafted to defeat this loop's bounds assumption. [3](#0-2) 

### Impact Explanation
An out-of-bounds read in a native (C++) parser can crash the build (denial of service is explicitly out of scope) but more importantly can read adjacent heap memory into fields (`header_id`, `data_size`, and subsequently `uncompressed_size`/`compressed_size`/`offset` when the ZIP64 tag is hit) that are later used to drive allocation sizes and `memcpy` lengths in the same extraction pipeline (e.g., `Decompressor::UncompressFile`, `TransientBytes::DecompressEntryContents`). Corrupted size/offset values derived from out-of-bounds memory can propagate into subsequent buffer-size decisions, creating a path toward memory corruption beyond a simple read, matching the severity class of the referenced CVE (heap-based buffer overflow in an archive/text parser).

### Likelihood Explanation
Any repository consuming a `http_jar`, `http_archive`, or Maven-resolved jar with a pinned `sha256`/`integrity` value is exposed if the origin (or a compromised mirror/registry entry whose hash was recorded from a version the attacker also controls) serves a specially crafted jar. Since `ijar` runs automatically on essentially every external Java compile-time dependency, no special build configuration is required to reach this code — the attacker only needs to control the bytes of an artifact whose hash is trusted by the victim's build.

### Recommendation
Add explicit bounds checks in the extra-field parsing loop of `InputZipFile::ProcessCentralDirEntry` (and any equivalent loop in `third_party/ijar/zip.cc`): before reading `header_id`/`data_size`, verify at least 4 bytes remain between `extra_p` and `p`; after computing `data_size`, verify `extra_p + data_size <= p` before advancing, and abort/return an error via the existing `error()` mechanism otherwise (mirroring the safer `ExtraField::find` bounds handling already used in `src/tools/singlejar/zip_headers.h`).

### Proof of Concept
Extend the existing `third_party/ijar/test/ijar_test.sh` central-directory regression coverage (see the existing `test_wrong_centraldir` test using a crafted "JAR_WRONG_CENTRAL_DIR" fixture) with a new fixture jar whose central-directory entry has an `extra_field_length` set, and whose contained extra-field record declares a `data_size` that does not evenly divide/terminate at `extra_field_length` (e.g., a single record with `data_size = 0xFFFF` inside a 10-byte extra-field block). Running the `ijar` binary (or a JUnit test invoking `ZipExtractor::Create`/`ProcessAll` directly) over this fixture should be shown, under an ASan build, to trigger a heap-buffer-overflow read report instead of a clean parse error, confirming the missing bounds check identified above.

### Citations

**File:** third_party/ijar/zip.cc (L507-542)
```text
  p += 16;  // skip to 'compressed size' field
  *compressed_size = get_u4le(p);
  *uncompressed_size = get_u4le(p);
  u2 file_name_length = get_u2le(p);
  u2 extra_field_length = get_u2le(p);
  u2 file_comment_length = get_u2le(p);
  p += 4;  // skip to external file attributes field
  *attr = get_u4le(p);
  *offset = get_u4le(p);
  {
    size_t len = (file_name_length < filename_size)
      ? file_name_length
      : (filename_size - 1);
    memcpy(reinterpret_cast<void*>(filename), p, len);
    filename[len] = 0;
  }
  p += file_name_length;
  const u1 *extra_p = p;
  p += extra_field_length;
  while (extra_p != p) {
    const u2 header_id = get_u2le(extra_p);
    const u2 data_size = get_u2le(extra_p);
    const u1 *extra = extra_p;
    extra_p += data_size;
    if (header_id == ZIP64_EXTRA_FIELD_TAG) {
      if (*uncompressed_size == U4_MAX) {
        *uncompressed_size = get_u8le(extra);
      }
      if (*compressed_size == U4_MAX) {
        *compressed_size = get_u8le(extra);
      }
      if (*offset == U4_MAX) {
        *offset = get_u8le(extra);
      }
    }
  }
```

**File:** tools/build_defs/repo/http.bzl (L340-358)
```text
def _http_jar_impl(ctx):
    """Implementation of the http_jar rule."""
    source_urls = _get_source_urls(ctx)
    downloaded_file_name = ctx.attr.downloaded_file_name
    download_info = ctx.download(
        source_urls,
        "jar/" + downloaded_file_name,
        ctx.attr.sha256,
        canonical_id = ctx.attr.canonical_id or get_default_canonical_id(ctx, source_urls),
        auth = get_auth(ctx, source_urls),
        integrity = ctx.attr.integrity,
    )
    ctx.file("WORKSPACE", "workspace(name = \"{name}\")".format(name = ctx.name))
    ctx.file("jar/BUILD", _HTTP_JAR_BUILD.format(
        java_import_bzl = str(Label("@rules_java//java:java_import.bzl")),
        file_name = downloaded_file_name,
    ))

    return _update_integrity_attr(ctx, _http_jar_attrs, download_info)
```
