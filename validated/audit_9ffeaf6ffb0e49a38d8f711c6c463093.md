### Title
Heap buffer over-read parsing an attacker-crafted ZIP64 End-of-Central-Directory locator offset - (File: `third_party/ijar/zip.cc`)

### Summary
`FindZipCentralDirectory` in `third_party/ijar/zip.cc` locates a ZIP's End-of-Central-Directory (EOCD) record and, when present, walks into a ZIP64 EOCD locator/record via `FindZip64CentralDirectory` / `MaybeReadZip64CentralDirectory`. The candidate ZIP64 EOCD position is computed from an **attacker-controlled 8-byte offset field taken directly from the ZIP64 EOCD locator** and is only checked against the *lower* bound of the mapped buffer (`current < bytes`), never against the *upper* bound (`bytes + in_length`). A crafted archive can therefore make the parser read the fixed 56-byte ZIP64 EOCD structure (`get_u8le`/`get_u4le`/`get_u2be` calls) far past the end of the memory-mapped input file, exactly analogous to the unchecked multi-byte little/big-endian reads in UPX's `get_ne64` (`bele.h`) that caused CVE-2024-3209.

### Finding Description
`third_party/ijar/zip.cc:702-778` locates the standard EOCD, then calls: [1](#0-0) 

`FindZip64CentralDirectory` first tries a fixed-offset guess, bounded correctly (`current < bytes` check), then falls back to the ZIP64 locator path: [2](#0-1) 

`zip64_end_of_central_dir_offset` is read straight from the locator bytes with `get_u8le`, an attacker-fully-controlled 64-bit value, and is used unchecked to compute `bytes + zip64_end_of_central_dir_offset`, which is then passed as `current` into `MaybeReadZip64CentralDirectory`: [3](#0-2) 

That function validates only `current < bytes` (the lower bound) before dereferencing 56 bytes via `get_u8le`/`get_u4le`/`get_u2be`: [4](#0-3) 

There is no check that `current + ZIP64_EOCD_FIXED_SIZE <= bytes + in_length`. Since `zip64_end_of_central_dir_offset` is an unbounded `u8` taken from attacker bytes, `bytes + zip64_end_of_central_dir_offset` can point arbitrarily far beyond (or, via pointer overflow, effectively anywhere relative to) the end of the `mmap`'d region backing the input file (`MappedInputFile::Buffer()`), producing a heap/mapped-memory out-of-bounds read when the signature and subsequent 8 fields are decoded — the same bug class as get_ne64 reading a little-endian 64-bit word without validating the source buffer bounds. Contrast this with the local-file-entry path, `InputZipFile::ProcessLocalFileEntry`, which explicitly bounds-checks with `EnsureRemaining` before every multi-byte read; the ZIP64 EOCD path has no equivalent guard.

### Impact Explanation
This code runs whenever Bazel processes a JAR/ZIP through `ijar` (used to build interface jars from Java compilation outputs and library archives). An externally supplied, fully checksum-verified archive (e.g., a `.jar` obtained via `http_jar`/`http_archive` with a correct `sha256`/`integrity` field, or a jar produced from an untrusted CI branch) can still contain a fully valid-checksum but structurally malicious ZIP64 locator whose offset field triggers the out-of-bounds read described above. The result is a heap/mapped-memory over-read: at minimum a crash (denial of service against the local `ijar` process during the build), and potentially disclosure of adjacent process memory if any parsed field (e.g., derived "central directory size/offset" comparisons or downstream error messages that embed offsets) is echoed back — matching the "heap-based buffer overflow" (over-read) class of CVE-2024-3209.

### Likelihood Explanation
Any attacker who can publish a `.jar`/`.zip` file consumed by a build (via a dependency URL, mirror, or committed test/build artifact) — without needing to control the victim's machine or bypass the declared checksum — can trigger this: the checksum only guarantees the bytes match what was pinned, not that the bytes are a well-formed ZIP64 archive. Crafting the specific 8-byte offset in the ZIP64 EOCD locator to point outside the buffer is a single-field manipulation, making exploitation straightforward once a hostile artifact is ingested.

### Recommendation
In `MaybeReadZip64CentralDirectory` (and its caller `FindZip64CentralDirectory`), validate the upper bound before dereferencing: require `current + ZIP64_EOCD_FIXED_SIZE <= bytes + in_length` (in addition to the existing `current < bytes` check), and thread `in_length` through so the locator-derived offset (`zip64_end_of_central_dir_offset`) is range-checked against the actual mapped file length before being used to compute a pointer, mirroring the `EnsureRemaining` bounds-checking pattern already used in `InputZipFile::ProcessLocalFileEntry`.

### Proof of Concept
Construct a minimal ZIP file consisting of: a valid standard EOCD record (with `comment_length` matching the file end, as required by `FindZipCentralDirectory`), immediately preceded by a valid ZIP64 EOCD Locator (`0x07064b50` signature) whose "offset of zip64 end of central directory record" field is set to a huge value (e.g., `0xFFFFFFF0`), well beyond the actual file size. Feed this file to `ijar`/`zipper` (or any Bazel build step invoking `ZipExtractor::Create`, e.g. compiling a `java_library` that depends on such a jar, or `singlejar`'s analogous jar processing). A `BuildIntegrationTestCase`/`src/test/shell/bazel` shell test can assert that `ijar` either crashes (SIGSEGV) or returns a spurious error derived from out-of-bounds memory rather than a clean "invalid archive" diagnostic, demonstrating the unguarded read past `bytes + in_length`.

### Citations

**File:** third_party/ijar/zip.cc (L595-621)
```text
bool MaybeReadZip64CentralDirectory(const u1 *bytes, size_t /*in_length*/,
                                    const u1 *current,
                                    const u1 **end_of_central_dir,
                                    EndOfCentralDirectoryRecord *cd) {
  if (current < bytes) {
    return false;
  }
  const u1 *candidate = current;
  u4 zip64_directory_signature = get_u4le(current);
  if (zip64_directory_signature != ZIP64_EOCD_SIGNATURE) {
    return false;
  }

  // size of zip64 end of central directory record
  // (fixed size unless there's a zip64 extensible data sector, which
  // we don't need to read)
  get_u8le(current);
  get_u2be(current);  // version made by
  get_u2be(current);  // version needed to extract

  u4 number_of_this_disk = get_u4be(current);
  u4 disk_with_central_dir = get_u4le(current);
  u8 central_dir_entries_on_this_disk = get_u8le(current);
  u8 central_dir_entries = get_u8le(current);
  u8 central_dir_size = get_u8le(current);
  u8 central_dir_offset = get_u8le(current);

```

**File:** third_party/ijar/zip.cc (L678-691)
```text
  const u1 *zip64_locator = *end_of_central_dir - ZIP64_EOCD_LOCATOR_SIZE;
  if (zip64_locator - ZIP64_EOCD_FIXED_SIZE < bytes) {
    return true;
  }
  u4 zip64_locator_signature = get_u4le(zip64_locator);
  if (zip64_locator_signature != ZIP64_EOCD_LOCATOR_SIGNATURE) {
    return true;
  }
  u4 disk_with_zip64_central_directory = get_u4le(zip64_locator);
  u8 zip64_end_of_central_dir_offset = get_u8le(zip64_locator);
  u4 zip64_total_disks = get_u4le(zip64_locator);
  if (MaybeReadZip64CentralDirectory(bytes, in_length,
                                     bytes + zip64_end_of_central_dir_offset,
                                     end_of_central_dir, cd)) {
```

**File:** third_party/ijar/zip.cc (L756-758)
```text
  if (!FindZip64CentralDirectory(bytes, in_length, &end_of_central_dir, &cd)) {
    return false;
  }
```
