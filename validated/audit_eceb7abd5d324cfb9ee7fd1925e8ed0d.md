This confirms `ijar` (`third_party/ijar/zip.cc`) is invoked by `java_common.run_ijar` on jar files provided as build inputs — including jars downloaded via `http_jar`/`http_archive`/Maven-style dependencies, which can come from an untrusted origin/registry. The `EnsureRemaining` bounds check is the analog of the reported "avoid overflow on bound check" bug class.

### Title
Integer underflow in `InputZipFile::EnsureRemaining` bound check allows out-of-bounds read when processing attacker-supplied JAR/ZIP files - (File: `third_party/ijar/zip.cc`)

### Summary
`ijar`'s ZIP parser computes remaining-bytes-in-file using unsigned subtraction without first validating that the cursor is still within the mapped file. A malicious jar (e.g., fetched via `http_jar`, `http_archive`, or a Maven/BCR artifact and then processed through `run_ijar`/interface-jar generation) can supply a central-directory "local header offset" or size field that pushes the read cursor past `input_file_->Length()`. The subsequent `EnsureRemaining` check underflows and wrongly reports "enough bytes remaining," permitting the parser to read (and copy into `filename`, or feed to zlib) memory beyond the mmap'd input buffer.

### Finding Description
`InputZipFile::EnsureRemaining` is defined as: [1](#0-0) 

```cpp
int EnsureRemaining(size_t n, const char *state) {
  size_t in_offset = p - zipdata_in_;
  size_t remaining = input_file_->Length() - in_offset;
  if (n > remaining) { ... error ... }
  return 0;
}
```

`in_offset` and `remaining` are `size_t` (unsigned). The cursor `p` is set from attacker-controlled central-directory fields with no prior range check: [2](#0-1) 

`*offset` (local-header offset) and the ZIP64 extension fields (`get_u8le(extra)`) in `ProcessCentralDirEntry` are read directly from file bytes with no bound validation against the mapped file size: [3](#0-2) 

If an attacker sets `offset` (or a ZIP64 64-bit offset/size override) larger than `input_file_->Length()`, then `p = zipdata_in_ + in_offset_ + offset` points past the end of the mapped region, making `in_offset > input_file_->Length()`. The subtraction `input_file_->Length() - in_offset` then wraps around to a huge `size_t` value instead of failing, so `EnsureRemaining` incorrectly reports abundant remaining bytes. This same pattern recurs at every call site — `file_name_length`, `extra_field_length`, `compressed_size_` in `ProcessLocalFileEntry` / `SkipFile`: [4](#0-3) [5](#0-4) 

Once the bogus "remaining" check passes, the code proceeds to `memcpy`/decompress from `p`, an out-of-bounds pointer, e.g. in `ProcessCentralDirEntry`'s filename copy and in `UncompressFile`'s call into zlib with an attacker-influenced `remaining` size.

### Impact Explanation
This is an out-of-bounds heap/mmap read reachable purely from untrusted archive bytes processed at build time (e.g., through `http_jar`, `java_import` of a fetched jar, or any dependency whose jar is passed to `run_ijar`/`ijar` for interface-jar generation, which Bazel does routinely for Java compilation dependencies). The read can crash the build (denial of service) or leak adjacent heap memory into the ijar output/error messages (the `filename` buffer copy in `ProcessCentralDirEntry` is bounded by `PATH_MAX`, but the source pointer itself is out of bounds, and subsequent decompression can pass arbitrarily large "remaining" sizes into zlib). This matches "read outside the repository/output base" in spirit — it's a read from outside the intended input buffer triggered by attacker-controlled content that a checksum-verified download still allows (checksum only verifies the whole file's integrity, not the internal offsets used during parsing).

### Likelihood Explanation
Any Bazel Java build that consumes an externally-fetched `.jar` (via `http_jar`, `http_file` + `java_import`, Maven artifacts through `rules_jvm_external`, etc.) and runs it through `ijar` (the default behavior for generating interface jars for compile-time dependencies) will invoke this code path on fully attacker-controlled bytes. The attacker only needs to control the contents of a `.jar` served at a URL/registry the victim's build fetches — no need to break checksum verification, since the checksum only guarantees byte-for-byte integrity of the malicious file itself.

### Recommendation
Fix `EnsureRemaining` to check the invariant `in_offset <= input_file_->Length()` before performing the subtraction, and reject/bail out with an error if `p` is already past the end of file (mirroring the upstream kernel fix's approach: validate before subtracting, not after). Apply the same de-underflow discipline to `p = zipdata_in_ + in_offset_ + offset` in `ProcessNext` (validate `offset` against `Length()` before dereferencing) and to the ZIP64 override path in `ProcessCentralDirEntry`.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` reproduction would: (1) construct a minimal ZIP with one central-directory entry whose local-header `offset` field (or ZIP64 64-bit offset override) is set to a value larger than the file's actual length; (2) run `ijar`/`run_ijar` (or `bazel build` on a `java_import` wrapping this crafted jar with `run_ijar` enabled) and observe a crash (heap-buffer-overflow under ASan) or non-deterministic output instead of a clean parse error, demonstrating that `EnsureRemaining`'s underflowed check fails to reject the malformed offset.

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

**File:** third_party/ijar/zip.cc (L302-318)
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
```

**File:** third_party/ijar/zip.cc (L360-370)
```text
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

**File:** third_party/ijar/zip.cc (L420-434)
```text
int InputZipFile::SkipFile(const bool compressed) {
  if (!compressed) {
    // In this case, compressed_size_ == uncompressed_size_ (since the file is
    // uncompressed), so we can use either.
    if (compressed_size_ != uncompressed_size_) {
      return error("compressed size != uncompressed size, although the file "
                   "is uncompressed.\n");
    }
  }

  if (EnsureRemaining(compressed_size_, "file_data") < 0) {
    return -1;
  }
  p += compressed_size_;
  return 0;
```

**File:** third_party/ijar/zip.cc (L507-541)
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
```
