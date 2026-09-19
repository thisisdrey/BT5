## Analog Vulnerability Found

### Title
Out-of-bounds read from unchecked ZIP extra-field length in `ProcessCentralDirEntry()` - (File: `third_party/ijar/zip.cc`)

### Summary
The upstream CVE-2025-37998 fix replaced manual, unchecked iteration over nested Netlink attributes with a bounds-validating iterator. Bazel's ijar/singlejar ZIP parser has the analogous defect: `InputZipFile::ProcessCentralDirEntry()` manually walks a length-prefixed "extra fields" sub-structure inside the ZIP central directory using attacker-controlled 16-bit length fields, with no bounds validation against the actual mapped buffer.

### Finding Description
`ProcessCentralDirEntry` reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly from the memory-mapped ZIP bytes and advances the cursor `p` by `extra_field_length` unconditionally: [1](#0-0) 

It then manually iterates the extra-field sub-records: [2](#0-1) 

Each iteration reads a `header_id`/`data_size` pair and advances `extra_p += data_size` without ever checking that `extra_p + 4 <= p` before the read, or that `extra_p + data_size <= p` after it. If `data_size` for one sub-record is crafted larger than the remaining extra-field area, `extra_p` can overshoot `p` (making `extra_p != p` never terminate normally, or wrap past the mapped region) and `get_u8le(extra)` for a spoofed `ZIP64_EXTRA_FIELD_TAG` record reads 8 bytes from an address outside the declared extra-field bounds.

This is unlike `InputZipFile::ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before consuming `file_name_length` and `extra_field_length`: [3](#0-2) 

`ProcessCentralDirEntry` has no equivalent `EnsureRemaining` calls at all — none of `file_name_length`, `extra_field_length`, `file_comment_length`, nor the inner extra-field walk are validated against the memory-mapped file's actual size, even though the entire ZIP file is mmap'd read-only input from an untrusted archive: [4](#0-3) 

The comment on the function claims parsing is "safe" because the central directory is always followed by a signature-bearing structure, but that assumption does not hold once a malformed `extra_field_length`/inner `data_size` pushes `extra_p`/`p` past the actual end of the memory-mapped region — nothing stops the read from crossing the mapping boundary. [5](#0-4) 

### Impact Explanation
`third_party/ijar/zip.cc` backs both `ijar` (interface-jar generation) and `singlejar`, which consume ZIP/JAR files that can originate from external, attacker-influenced sources (e.g., artifacts fetched via `http_jar`/`http_archive`/Maven-style dependency rules whose bytes are attacker-served but hash-verified). Because the sha256/integrity check only verifies the byte stream matches a fixed hash — it does not validate ZIP internal structural invariants — an attacker who controls the served archive content (and knows/controls the resulting hash, e.g. for a new dependency version or an unpinned URL) can craft a central directory entry whose extra-field length lies about its size, causing out-of-bounds reads during interface-jar/singlejar processing. This is a memory-safety violation (OOB read, potential crash or information disclosure) in a C++ tool that runs as part of the build with the invoking user's privileges.

### Likelihood Explanation
Reaching this code only requires supplying a ZIP/JAR as a build input (source jar for `ijar`, or one of `singlejar`'s `--sources`), which is routine for any Bazel Java build consuming external dependencies. The parser executes unconditionally on any `.jar`/`.zip` processed and performs no validation of nested extra-field lengths against the mapped buffer size, so a single malformed archive triggers the bug deterministically.

### Recommendation
Add explicit bounds checks in `ProcessCentralDirEntry` mirroring `EnsureRemaining` used in `ProcessLocalFileEntry`:
- Validate `file_name_length`, `extra_field_length`, and `file_comment_length` against the actual remaining bytes in the mapped central directory before advancing `p`.
- In the extra-field walk, before each read verify `extra_p + 4 <= p` (header) and after reading `data_size`, verify `extra_p + data_size <= p` before advancing, aborting/erroring otherwise instead of silently overrunning.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` style reproduction: construct a ZIP file whose central directory entry declares `extra_field_length` covering a single extra-field sub-record with `header_id = ZIP64_EXTRA_FIELD_TAG` and a `data_size` value that, combined with the sub-record start offset, exceeds the actual extra-field/central-directory region (analogous to the existing `CreateZipWithMalformedExtraField` pattern already used for the singlejar `zip_headers.h` extra-field parser regression test at `src/tools/singlejar/output_jar_simple_test.cc:1179-1236`, but targeting `ijar`'s `ProcessCentralDirEntry` instead), then run `ijar`/`singlejar` against it and observe the out-of-bounds read (e.g., under ASan) instead of a controlled error. [6](#0-5) [7](#0-6)

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

**File:** third_party/ijar/zip.cc (L486-545)
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

**File:** third_party/ijar/zip.h (L1-1)
```text
// Copyright 2015 The Bazel Authors. All rights reserved.
```
