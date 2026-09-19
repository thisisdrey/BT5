## Title
Out-of-bounds heap read in ijar's ZIP central-directory parser via unchecked ZIP64 extra-field size — (File: `third_party/ijar/zip.cc`)

## Summary
`InputZipFile::ProcessCentralDirEntry` in ijar's ZIP parser reads a fixed 8-byte little-endian value out of a ZIP64 extra-field payload without verifying that the payload actually contains 8 bytes, and without validating `extra_field_length`/`file_name_length` against the remaining bytes in the central directory at all (unlike the local-file-header parser, which does call `EnsureRemaining`). A crafted `.jar`/`.zip` file can set an oversized/garbage 64-bit "relative offset of local header" via a truncated ZIP64 extra field, which is later used to compute a raw pointer into the mapped file. The subsequent bounds check (`EnsureRemaining`) uses unsigned pointer-difference arithmetic that wraps around when the computed pointer exceeds the mapped file, causing the check to incorrectly pass and dereference memory outside of the mapped ZIP file.

## Finding Description
`ProcessCentralDirEntry` parses the ZIP64 extra field like this: [1](#0-0) 

For each extra-field record it reads a 2-byte `header_id` and 2-byte `data_size`, then — if `header_id == ZIP64_EXTRA_FIELD_TAG` — unconditionally calls `get_u8le(extra)` up to three times (for uncompressed size, compressed size, and offset) without ever checking that `data_size` is large enough (it should be 8 for each populated 64-bit field, up to 24 total) to contain the requested 8-byte values. This is a classic type/size-confusion bug: the code assumes a fixed 8-byte quantity is present because the tag says "ZIP64", but the attacker fully controls `data_size` and the bytes that follow.

Compare this to the local-file-header parser, `ProcessLocalFileEntry`, which does call `EnsureRemaining` before reading `file_name_length` / `extra_field_length` bytes: [2](#0-1) 

`ProcessCentralDirEntry` has no equivalent check anywhere — it relies purely on a comment claiming "the central directory is always followed by another data structure that has a signature, so parsing it this way is safe": [3](#0-2) 

The parsed `*offset` value (attacker-controlled, and now possibly built from unrelated/garbage bytes if `data_size < 8`) is subsequently used, unchecked, to seek an absolute pointer into the mapped file: [4](#0-3) 

The only guard before dereferencing `p` is `EnsureRemaining`: [5](#0-4) 

`EnsureRemaining` computes `in_offset = p - zipdata_in_` and `remaining = input_file_->Length() - in_offset`, both as `size_t` (unsigned). If the attacker-controlled `offset` used to build `p` is large enough that `p` falls beyond `zipdata_in_ + input_file_->Length()`, then `remaining` underflows to a huge value, so `n > remaining` is false and the check incorrectly succeeds. `get_u4le(p)` at line 319 then dereferences memory outside the mmap'd ZIP file region — an out-of-bounds heap read driven by a size/type-confusion in the ZIP64 extra-field parsing, the same root-cause pattern as the libxslt `xsltNumberFormatGetMultipleLevel` bug (a crafted length/type value causes the parser to read past the bounds implied by the real data).

## Impact Explanation
ijar processes third-party `.jar`/`.zip` files that are not necessarily produced or reviewed by the build author — for example jars fetched via `http_jar`/`http_archive`, `java_import`, or Maven/registry dependencies whose sha256/integrity may not be enforced for every consumer of that content, or where the content is later re-verified only at a coarser granularity. An attacker who controls the bytes of such a jar can trigger an out-of-bounds heap read in the `ijar` binary that runs as part of the build (interface-jar generation). Depending on process memory layout this results in a crash of the ijar action (denial of service for that action) or, if the OOB pointer happens to land on mapped-but-unintended memory, a heap-memory disclosure that can leak adjacent process memory bytes into the derived interface jar output that is then consumed by the rest of the build graph — i.e., untrusted attacker content is allowed to escape its expected data role and corrupt the memory-safety invariant that ZIP metadata fields are validated before being trusted as offsets/lengths.

## Likelihood Explanation
The bug is trivially reachable: any `.jar`/`.zip` that reaches `ijar` (or any tool linking `third_party/ijar/zip.cc`, e.g. Bazel's own `ijar` binary used for every `java_library`'s interface jar and for extracting bootstrap archives) with a crafted central-directory record — file-name field, a ZIP64 extra field whose declared `data_size` is smaller than 8 bytes, and a 32-bit size/offset field set to `0xFFFFFFFF` to force the ZIP64 fallback path — will exercise this code path. No credentials, no MITM, and no privileged access are required; only the ability to have the victim's build fetch/consume an attacker-authored archive.

## Recommendation
- In `ProcessCentralDirEntry`, validate `extra_field_length` against the actual number of bytes remaining before the file comment/next signature (mirroring the `EnsureRemaining` checks already used in `ProcessLocalFileEntry`).
- Before calling `get_u8le(extra)` for any ZIP64 sub-field, verify `data_size >= 8` (and that `extra + 8 <= extra_p_end`) for each 64-bit attribute actually consumed.
- Fix `EnsureRemaining` to detect the underflow case explicitly (e.g., check `p < zipdata_in_ || p > zipdata_in_ + input_file_->Length()` before computing `remaining`), so a corrupted offset cannot make the bounds check pass.

## Proof of Concept
Construct a `.jar`/`.zip` with:
1. A central directory record whose 32-bit `compressed_size`, `uncompressed_size`, and `relative offset of local header` fields are all `0xFFFFFFFF` (forcing use of the ZIP64 extra field per lines 532-540).
2. A ZIP64 extra field (`header_id = 0x0001`) with `data_size` set to `0` (or any value `< 24`), i.e. no actual payload bytes, immediately followed by attacker-chosen bytes (e.g. from the file-comment region) that, when read as consecutive `get_u8le` calls, produce a huge 64-bit `offset` value.
3. Feed this file to `ijar` (or a `BuildIntegrationTestCase`/`src/tools/singlejar`-style test invoking `devtools_ijar::ZipExtractor::Create` + `ProcessAll()` on the crafted bytes).

Expected: `ProcessNext` computes `p = zipdata_in_ + in_offset_ + offset` with `offset` far exceeding `input_file_->Length()`; `EnsureRemaining(4, "signature")` underflows and returns success; `get_u4le(p)` dereferences memory outside the mapped file, producing a segfault (observable via `ASSERT_DEATH`/crash in a `src/test/shell/bazel` or C++ unit test) or non-deterministic garbage read depending on address-space layout — demonstrating the OOB read is reachable purely from crafted archive bytes.

### Citations

**File:** third_party/ijar/zip.cc (L161-170)
```text
  int EnsureRemaining(size_t n, const char *state) {
    size_t in_offset = p - zipdata_in_;
    size_t remaining = input_file_->Length() - in_offset;
    if (n > remaining) {
      return error("Premature end of file (at offset %zd, state=%s); "
                   "expected %zd more bytes but found %zd.\n",
                   in_offset, state, n, remaining);
    }
    return 0;
  }
```

**File:** third_party/ijar/zip.cc (L302-319)
```text
bool InputZipFile::ProcessNext() {
  // Process the next entry in the central directory. Also make sure that the
  // content pointer is in sync.
  u8 compressed, uncompressed;
  u8 offset;
  if (!ProcessCentralDirEntry(central_dir_current_, &compressed, &uncompressed,
                              filename, PATH_MAX, &attr, &offset)) {
    return false;
  }

  // There might be an offset specified in the central directory that does
  // not match the file offset, so always update our pointer.
  p = zipdata_in_ + in_offset_ + offset;

  if (EnsureRemaining(4, "signature") < 0) {
    return false;
  }
  u4 signature = get_u4le(p);
```

**File:** third_party/ijar/zip.cc (L357-370)
```text
  file_name_length_ = get_u2le(p);
  extra_field_length_ = get_u2le(p);

  if (EnsureRemaining(file_name_length_, "file_name") < 0) {
    return -1;
  }
  file_name_ = p;
  p += file_name_length_;

  if (EnsureRemaining(extra_field_length_, "extra_field") < 0) {
    return -1;
  }
  extra_field_ = p;
  p += extra_field_length_;
```

**File:** third_party/ijar/zip.cc (L486-497)
```text
// - whether the entry is a class file (to be included in the output).
// Precondition: p points to the beginning of an entry in the central dir
// Postcondition: p points to the beginning of the next entry in the central dir
// Returns true if the central directory contains another file and false if not.
// Of course, in the latter case, the size output variables are not changed.
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
bool InputZipFile::ProcessCentralDirEntry(const u1 *&p, u8 *compressed_size,
                                          u8 *uncompressed_size, char *filename,
                                          size_t filename_size, u4 *attr,
                                          u8 *offset) {
  u4 signature = get_u4le(p);
```

**File:** third_party/ijar/zip.cc (L524-542)
```text
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
