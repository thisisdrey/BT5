### Title
Missing bounds checks when parsing ZIP central-directory entries allows out-of-bounds read past the mapped JAR file - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` reads variable-length fields (`file_name_length`, `extra_field_length`, `file_comment_length`, and nested ZIP64 extra-field entries) from a memory-mapped, attacker-influenced JAR/ZIP and advances its cursor by these attacker-controlled lengths without ever checking that the cursor stays inside the mapped file, unlike the local-file-header path which calls `EnsureRemaining()` before every read.

### Finding Description
`ProcessCentralDirEntry` reads the fixed 46-byte central directory header fields directly with `get_u4le`/`get_u2le` and then does: [1](#0-0) 
It advances `p` by `file_name_length`, then walks `extra_field_length` bytes of extra fields via `extra_p += data_size;` in a loop bounded only by `extra_p != p`, and finally advances by `file_comment_length` — none of these three attacker-controlled 16-bit lengths are checked against the actual remaining size of the memory-mapped input file: [2](#0-1) 

This is in sharp contrast to `ProcessLocalFileEntry`, which explicitly guards every read with `EnsureRemaining()`: [3](#0-2) [4](#0-3) 

`FindZipCentralDirectory` only validates that the central directory's *total* offset+size stays within the file bounds; it never validates the internal structure of each entry: [5](#0-4) 

Consequently, a hostile ZIP/JAR (e.g., served as a `java_import` prebuilt jar, or produced by an untrusted-branch build and consumed by `ijar`/`zipper`) can declare a `file_name_length`, `extra_field_length`, or `file_comment_length` in the last central-directory entry that runs past the end of the mmap'd region, causing `ProcessCentralDirEntry` (called from `InputZipFile::ProcessNext` and `InputZipFile::CalculateOutputLength`) to read out-of-bounds memory beyond the mapped file — the same bug class as the OpenEXR `DwaCompressor::Classifier` off-by-one: attacker-controlled length fields drive a read cursor past the buffer without a bounds check.

### Impact Explanation
An out-of-bounds read in `ijar`/`zipper` (used to build interface jars and to pack/unpack jars during Java compilation) could crash the build (denial of service) or leak adjacent process memory contents (e.g., via the copied filename into `errmsg`/diagnostics, or via `ProcessCentralDirEntry`'s copy of `file_name_length` bytes into a fixed `filename[PATH_MAX]` buffer, which is itself a buffer over-read if `file_name_length` exceeds `filename_size`). This is a memory-safety violation in a native (C++) binary invoked as part of ordinary Bazel Java build actions, matching the "read outside the repository/exec root/output base" (mmap'd file bounds) impact bucket.

### Likelihood Explanation
The vulnerable path is reachable by any input JAR/ZIP that Bazel's `ijar` tool processes as a dependency (prebuilt jars supplied via `java_import`, or any jar an attacker can get built/consumed without full recompilation from source). No checksum, containment, or credential mechanism inspects the internal ZIP structure fields before ijar parses them — a `sha256`/`integrity` pin on the *download* only prevents byte-level tampering of a specific pinned artifact; it does not stop an attacker who is the legitimate publisher of a new/unpinned artifact version, nor artifacts consumed via mechanisms without a required checksum (e.g. `local_repository`/build outputs from an untrusted branch). The bug requires no privileged access — only that the victim's build processes the crafted file with `ijar`.

### Recommendation
Add explicit bounds checks (mirroring `EnsureRemaining()` in `ProcessLocalFileEntry`) inside `ProcessCentralDirEntry` before advancing past `file_name_length`, `extra_field_length`, `file_comment_length`, and each nested extra-field `data_size`, verifying the cursor never exceeds `bytes + in_length` for the mapped file. Additionally, verify `file_name_length < filename_size` before the `memcpy` into the fixed `filename` buffer (currently it silently truncates only in the copy but still advances `p` by the untruncated length, which is the concrete over-read vector).

### Proof of Concept
A minimal reproduction should be added as a C++ unit test (e.g., under `third_party/ijar/*_test.cc` or a new `zip_test.cc`) or a `src/test/shell/bazel` integration test that:
1. Crafts a ZIP file whose single central-directory entry declares `file_name_length = 0xFFFF` (or `extra_field_length`/`file_comment_length` set similarly large) while the actual file is only a few hundred bytes long, placing the bogus-length entry as the very last bytes of the mapped file.
2. Runs `ijar`/`zipper` (`InputZipFile::Open` + `ProcessAll`) against this crafted archive under AddressSanitizer.
3. Confirms ASan reports a heap-buffer-overflow / out-of-bounds read originating in `InputZipFile::ProcessCentralDirEntry`, demonstrating the read advances past the mmap'd file boundary.

Note: I was unable to fully verify whether a JUnit/shell integration test harness already exists in this indexed snapshot that exercises `ijar`'s central directory parser with malformed inputs — this should be confirmed by a background agent with full repository access before finalizing severity, since the index may not include every existing `third_party/ijar` test file.

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

**File:** third_party/ijar/zip.cc (L526-544)
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
  p += file_comment_length;
  return true;
```

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```
