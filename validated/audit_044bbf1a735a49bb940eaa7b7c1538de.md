### Title
Out-of-bounds read when parsing a crafted ZIP/JAR central directory in `ijar` - (File: `third_party/ijar/zip.cc`)

### Summary
`ijar`'s ZIP central-directory parser advances its read cursor using length fields taken directly from attacker-controlled bytes without ever re-validating that the cursor stays inside the memory-mapped input file. A crafted `.jar`/`.zip` dependency (e.g. a Maven/`http_archive` artifact whose bytes match a pinned but attacker-authored hash) can drive the parser to read past the end of the `mmap`'d buffer.

### Finding Description
`FindZipCentralDirectory` (third_party/ijar/zip.cc:704-778) validates only that `cd.central_dir_offset + cd.central_dir_size <= in_length` [1](#0-0)  before handing the central-directory pointer to the per-entry parser. It never re-checks bounds once entries are iterated one by one.

`InputZipFile::ProcessCentralDirEntry` (third_party/ijar/zip.cc:493-545) then reads `file_name_length`, `extra_field_length`, and `file_comment_length` as raw `u2` values straight from the archive and unconditionally advances the cursor `p` by their sum, with **no check that `p` remains within `bytes + in_length`**: [2](#0-1) . The nested extra-field walk is equally unchecked — it trusts an attacker-supplied `data_size` to advance `extra_p`, and only terminates when `extra_p == p` exactly, so a `data_size` that overshoots `p` causes the loop to keep dereferencing further out-of-bounds memory instead of stopping: [3](#0-2) .

This is in sharp contrast to `ProcessLocalFileEntry`, which guards every read with `EnsureRemaining()` against the *actual* file length before touching the buffer: [4](#0-3)  and [5](#0-4) . No equivalent guard exists for the central-directory entry parser, so a single crafted entry near the end of an otherwise-valid central directory can push `p`/`extra_p` past `zipdata_in_ + in_length`, and the very next `get_u4le`/`get_u2le` call dereferences memory outside the mapped file — an out-of-bounds read directly analogous to the V8 OOB read in the reported CVE, except here the "crafted page" is a crafted ZIP/JAR central directory consumed by Bazel's own `ijar` tool during a build (e.g. when generating interface jars for `java_import`/`java_library` from a downloaded `.jar`).

### Impact Explanation
`ijar` runs as part of ordinary Java build/interface-jar generation. An attacker who controls the content served at a dependency URL (mirror, registry, or any `http_archive`/`http_jar` target whose declared `sha256`/`integrity` they also control, e.g. a first-time or unpinned dependency) can supply a `.jar` whose central directory is well-formed enough to pass `FindZipCentralDirectory`'s coarse total-size check but contains an entry with oversized `file_name_length`/`extra_field_length`/`file_comment_length`/extra-field `data_size`. This causes `ijar` to read past the end of its mapped input buffer, leaking adjacent process memory into parsing decisions or crashing the build worker. This is a memory-safety violation in native (C++) code reachable purely from untrusted archive bytes, matching a Medium-severity OOB read.

### Likelihood Explanation
Reaching this code only requires publishing a `.jar`/`.zip` file at a URL a victim's build fetches and runs `ijar` (or `singlejar`, which shares similar unchecked-length parsing patterns) against — a routine occurrence for any Java dependency processed by Bazel. No special privileges, credentials, or MITM position are needed; the checksum mechanism only pins *which* bytes are fetched, not that those bytes are well-formed, so a hostile-but-hash-matching artifact is sufficient.

### Recommendation
Add explicit bounds checks (mirroring `EnsureRemaining`) in `InputZipFile::ProcessCentralDirEntry` before consuming `file_name_length`, `extra_field_length`, and `file_comment_length`, and bound the extra-field walk (`while (extra_p != p)`) by the mapped buffer's end (`zipdata_in_ + in_length`) rather than relying solely on exact equality with `p`. Reject the archive with a clear error instead of reading past the mapped region.

### Proof of Concept
A reproducible proof would live in a C++/`src/test/shell/bazel` test that:
1. Builds a valid minimal ZIP EOCD/central-directory whose total size satisfies `FindZipCentralDirectory`'s `central_dir_offset + central_dir_size <= in_length` check.
2. Crafts one central directory entry near the end of that region with `file_name_length = extra_field_length = file_comment_length = 0xFFFF` (or an extra field whose `data_size` overshoots the declared `extra_field_length`), so cumulative advancement of `p`/`extra_p` exceeds `in_length`.
3. Runs `ijar`'s `ZipExtractor::ProcessAll()` (via `InputZipFile::Open()` + `ProcessNext()`) under ASan/`--config=asan` against this crafted file and observes a heap-buffer-overflow (read) report at the `get_u4le`/`get_u2le` call inside `ProcessCentralDirEntry`, confirming the read escapes the `mmap`'d input buffer.

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

**File:** third_party/ijar/zip.cc (L507-523)
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

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```
