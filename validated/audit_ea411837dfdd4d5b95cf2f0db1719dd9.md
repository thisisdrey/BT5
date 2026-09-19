### Title
Out-of-bounds heap read when parsing crafted ZIP/JAR central directory entries in `ijar`'s `InputZipFile::ProcessCentralDirEntry` - (File: `third_party/ijar/zip.cc`)

### Summary
`third_party/ijar/zip.cc`'s `InputZipFile::ProcessCentralDirEntry` parses the Central Directory of a memory-mapped ZIP/JAR file without any bounds checking against the mapped file's extent, unlike its sibling `ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before every field read.

### Finding Description
`InputZipFile::Open()`/`LocateCentralDirectory()` locate the End-of-Central-Directory record and central directory offset/size fields from an mmap'd, attacker-influenced ZIP/JAR file (e.g., a JAR produced from a downloaded/external dependency that `ijar` is run against to strip interfaces, or any zip file `singlejar`/`ijar` processes). The loop that walks central directory entries repeatedly calls: [1](#0-0) 

Inside `ProcessCentralDirEntry`, fields are read directly with `get_u4le`/`get_u2le`/`get_u8le` and the cursor `p`/`extra_p` is advanced by attacker-controlled `file_name_length`, `extra_field_length`, and `file_comment_length` values taken straight from the crafted header — with **no call to `EnsureRemaining()`** and no check that `p` (or `extra_p`) stays within `mapped_file_`'s bounds: [2](#0-1) 

This is in sharp contrast to `ProcessLocalFileEntry`, which validates remaining bytes before every read: [3](#0-2) 

using the bounds-check helper: [4](#0-3) 

Because the central directory is memory-mapped input (`mapped_file_`, sized to the actual file), a crafted entry whose `file_name_length`/`extra_field_length`/`file_comment_length` (or a forged extra-field `data_size` in the ZIP64 loop at lines 526-541) point past the end of the mapping causes subsequent `get_u4le`/`get_u2le` calls, or the `memcpy` into the fixed `filename[PATH_MAX]` buffer, to read from unmapped or adjacent heap memory.

### Impact Explanation
An out-of-bounds read here can (a) crash the `ijar`/`singlejar` process (denial of service for the build), or (b) leak adjacent process memory into observable outputs — the `filename` buffer is later passed to `processor->Accept(filename, attr)` and can end up embedded in generated build artifacts (e.g., interface jar member names or paths), similar in class to the Electron advisory where out-of-bounds heap bytes leaked into an event handler. This is a genuine memory-safety violation triggered purely by feeding a crafted ZIP/JAR into `ijar`, requiring no local machine access, credentials, or privileged position — only a maliciously crafted archive (e.g., served as a dependency or supplied as build input).

### Likelihood Explanation
`ijar` is invoked on every JAR that is reduced to an interface jar, and `zip.cc`'s `InputZipFile` is the shared ZIP-reading code path for `ijar` (and structurally mirrored logic exists in `singlejar`'s `zip_headers.h`/`input_jar.cc`, though those files perform more careful bounds checks via `mapped_file_.mapped()`). Any build that processes a JAR/ZIP whose provenance is not fully trusted (e.g., a third-party `.jar` fetched as a dependency, or a JAR built from an attacker-influenced pipeline) reaches this code with attacker-controlled bytes with no sha256/mmap-bound gate specific to central-directory parsing. The missing check is a simple oversight (present in one sibling function but absent in the other), making the reachable condition (a truncated/short-mapped file plus an oversized length field in a central directory entry) straightforward to construct.

### Recommendation
Add the same `EnsureRemaining()`-style bounds validation used in `ProcessLocalFileEntry` to `ProcessCentralDirEntry`: before reading each fixed field, and before advancing `p`/`extra_p` by `file_name_length`, `extra_field_length`, `file_comment_length`, or any extra-field `data_size`, verify the advance stays within `zipdata_in_ + input_file_->Length()`. Reject (return `false`/emit an error) rather than continue parsing when a length field would run past the mapped file's end.

### Proof of Concept
Construct a minimal ZIP file where:
1. The End-of-Central-Directory (EOCD) record correctly locates a Central Directory Header (CDH).
2. The CDH's `file_name_length` (or `extra_field_length`/`file_comment_length`) field is set to a large value (e.g., `0xFFFF`) while the actual file on disk is truncated immediately after the CDH's fixed 46-byte portion, so the mapped file ends a few bytes past the CDH.
3. Run `ijar`/`singlejar`'s `InputZipFile::Open`+`ProcessNext` (or the equivalent `bazel`-invoked interface-jar-generation path) against this file.

Expected: `EnsureRemaining` in `ProcessLocalFileEntry` would reject a similarly truncated local header, but `ProcessCentralDirEntry` has no equivalent check, so `p += file_name_length` and the subsequent `memcpy(filename, p, len)` / `get_u2le(extra_p)` calls read past the mmap'd region, which can be observed as a crash (SIGSEGV/SIGBUS at a page boundary) or as garbage bytes copied into `filename`/`attr`, demonstrable via an ASan-instrumented build of `third_party/ijar:zip_test`/`ijar` binary reporting a heap-buffer-overflow on read.

(Note: I was unable to complete tracing every caller of `ProcessCentralDirEntry`, e.g. `CalculateOutputLength`, before running out of tool iterations, so the exact end-to-end invocation path from a Bazel build action into this code is not fully confirmed — this should be verified with a live session before treating the PoC as final.)

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

**File:** third_party/ijar/zip.cc (L493-545)
```text
bool InputZipFile::ProcessCentralDirEntry(const u1 *&p, u8 *compressed_size,
                                          u8 *uncompressed_size, char *filename,
                                          size_t filename_size, u4 *attr,
                                          u8 *offset) {
  u4 signature = get_u4le(p);

  if (signature != CENTRAL_FILE_HEADER_SIGNATURE) {
    if (signature != DIGITAL_SIGNATURE && signature != EOCD_SIGNATURE &&
        signature != ZIP64_EOCD_SIGNATURE) {
      error("invalid central file header signature: 0x%x\n", signature);
    }
    return false;
  }

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
  p += file_comment_length;
  return true;
}
```
