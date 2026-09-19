## Vulnerability Analog Found

### Title
Out-of-bounds read in `InputZipFile::ProcessCentralDirEntry` due to unvalidated attacker-controlled length fields - (File: `third_party/ijar/zip.cc`)

### Summary
`third_party/ijar/zip.cc`'s `InputZipFile::ProcessCentralDirEntry` parses ZIP/JAR central directory entries and their extra-field sub-records using length fields (`file_name_length`, `extra_field_length`, `file_comment_length`, and per-extra-field `data_size`) taken directly from the file bytes, with no check that the resulting cursor advance stays within the memory-mapped input buffer. This mirrors the krb5 CVE-2026-40356 class of bug: a length-driven parser that trusts attacker-controlled size fields without bounds validation, producing an out-of-bounds read when the parser walks a maliciously crafted network-delivered artifact.

### Finding Description
`ProcessCentralDirEntry` reads the fixed 46-byte central directory header fields, then: [1](#0-0) 
advances `p` by `file_name_length` and `extra_field_length` — both attacker-controlled `u2` values up to `0xffff` — with **no call to `EnsureRemaining`** (the bounds-check helper used elsewhere, e.g. in `ProcessLocalFileEntry`): [2](#0-1) [3](#0-2) 

The extra-field walking loop then repeatedly reads `header_id`/`data_size` and advances `extra_p += data_size` without ever checking that `extra_p` remains inside the mapped file: [4](#0-3) 

The only bounds check performed anywhere near central-directory parsing happens once, at `Open()` time, and only validates the *aggregate* claimed central-directory size against the file length — it does not validate that any individual entry's `file_name_length`/`extra_field_length`/`file_comment_length` fields are consistent with the remaining buffer: [5](#0-4) 

Because the input is accessed via `mmap` (`MappedInputFile`), advancing `p`/`extra_p` past `input_file_->Length()` reads into adjacent (possibly unmapped) memory pages — the same "attacker supplies a length field, no validation, downstream code walks the buffer using it and reads out of bounds" pattern as krb5's `parse_message` NegoEx underflow.

### Impact Explanation
A hostile origin that a victim's `http_jar`/`http_archive`/`java_import`-style dependency URL points to can serve a malicious `.jar`/`.zip` whose SHA-256 matches whatever hash the victim pins (the origin controls what bytes are served and thus what hash they publish for the victim to copy) — pinning a checksum only proves the *bytes fetched are the bytes the origin intended*, it does nothing to validate that a crafted central directory entry inside those bytes is well-formed. Once the checksum passes, Bazel's ijar tool (invoked to build interface jars from precompiled/fetched jars) parses the central directory with the vulnerable code above, causing an out-of-bounds read of process memory beyond the mapped file — a memory-safety violation (crash / potential information disclosure of adjacent memory), not merely a resource-exhaustion DoS.

### Likelihood Explanation
`ProcessLocalFileEntry` (a few dozen lines above) consistently calls `EnsureRemaining` before trusting any length field, showing the bounds-checking pattern is understood elsewhere in this file, but it was omitted for `ProcessCentralDirEntry`. Any jar processed by ijar — including externally fetched dependency jars — reaches this exact code path via `ZipExtractor::Create` → `ProcessNext`/`CalculateOutputLength` → `ProcessCentralDirEntry`, making the vulnerable path routinely exercised for external, potentially-attacker-influenced artifacts.

### Recommendation
Add `EnsureRemaining`-style bounds checks in `ProcessCentralDirEntry` before advancing `p` by `file_name_length`, `extra_field_length`, and `file_comment_length`, and before/while iterating the extra-field sub-records (bound `extra_p + data_size` and `extra_p + 4` by the declared `extra_field_length` and by the mapped-file end), returning a parse error instead of reading past the mapped buffer.

### Proof of Concept
A `BuildIntegrationTestCase`/unit test can construct a minimal ZIP whose central directory entry declares `extra_field_length = 0xffff` (or a nested extra-field `data_size` exceeding the remaining declared `extra_field_length`) while the physical file is only a few bytes past that entry (i.e., the entry sits at the very end of the mmap'd region). Running `ZipExtractor::Create`/`ProcessAll` (as `singlejar`/ijar callers do) on this file demonstrates `get_u2le`/`get_u4le`/`memcpy` in `ProcessCentralDirEntry` reading past `input_file_->Length()`, which under ASan/valgrind manifests as a heap/mmap out-of-bounds read (crash under a hardened allocator or `mmap` guard page).

### Citations

**File:** third_party/ijar/zip.cc (L158-170)
```text
  // Check that at least n bytes remain in the input file, otherwise
  // abort with an error message.  "state" is the name of the field
  // we're about to read, for diagnostics.
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

**File:** third_party/ijar/zip.cc (L332-370)
```text
int InputZipFile::ProcessLocalFileEntry(
    size_t compressed_size, size_t uncompressed_size) {
  if (EnsureRemaining(26, "extract_version") < 0) {
    return -1;
  }
  extract_version_ = get_u2le(p);
  general_purpose_bit_flag_ = get_u2le(p);

  if ((general_purpose_bit_flag_ & ~GENERAL_PURPOSE_BIT_FLAG_SUPPORTED) != 0) {
    return error("Unsupported value (0x%04x) in general purpose bit flag.\n",
                 general_purpose_bit_flag_);
  }

  compression_method_ = get_u2le(p);

  if (compression_method_ != COMPRESSION_METHOD_DEFLATED &&
      compression_method_ != COMPRESSION_METHOD_STORED) {
    return error("Unsupported compression method (%d).\n",
                 compression_method_);
  }

  // skip over: last_mod_file_time, last_mod_file_date, crc32
  p += 2 + 2 + 4;
  compressed_size_ = get_u4le(p);
  uncompressed_size_ = get_u4le(p);
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

**File:** third_party/ijar/zip.cc (L507-525)
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
```

**File:** third_party/ijar/zip.cc (L526-542)
```text
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

**File:** third_party/ijar/zip.cc (L766-778)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }

  // Do not change output values before determining that they are OK.
  *offset = cd.central_dir_offset;
  // Central directory start can then be used to determine the actual
  // starts of the zip file (which can be different in case of a non-zip
  // header like for auto-extractable binaries).
  *central_dir = end_of_central_dir - cd.central_dir_size;
  return true;
}
```
