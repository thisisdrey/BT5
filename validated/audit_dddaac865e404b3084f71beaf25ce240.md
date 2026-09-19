Based on my research, I found a concrete parsing bug in Bazel's own bundled jar/zip parser (`third_party/ijar/zip.cc`) that mirrors the PJSIP-style "parser lacks bounds validation on attacker-supplied length/offset fields" bug class.

### Title
Out-of-bounds read in ZIP central-directory parser due to missing length validation - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` parses the ZIP central directory of an archive (a `.jar`/`.zip` produced by ijar's `ZipExtractor`, used to build interface jars from build inputs) without any of the `EnsureRemaining()` bounds checks that its sibling `ProcessLocalFileEntry` performs [1](#0-0) . Fields such as `file_name_length`, `extra_field_length`, and the ZIP64 extra-field `data_size` are attacker-controlled and are used directly to advance pointers and read multi-byte integers from the mapped input buffer with no check against the file's actual mapped length [2](#0-1) .

### Finding Description
`EnsureRemaining()` is the only guard in this parser against reading past the end of the mmap'd input file, and it is called consistently in `ProcessLocalFileEntry` (e.g. before reading `extract_version`, `file_name`, `extra_field`) [3](#0-2) . In contrast, `ProcessCentralDirEntry` never calls `EnsureRemaining`, and blindly trusts `file_name_length`, `extra_field_length`, and `file_comment_length` read straight from the central directory record to advance the cursor `p` [4](#0-3) . Additionally, the ZIP64 extra-field walking loop advances `extra_p` by an attacker-controlled `data_size` and then unconditionally calls `get_u8le(extra)` when `header_id == ZIP64_EXTRA_FIELD_TAG`, without checking that at least 8 bytes remain within the declared extra-field region [5](#0-4) . A crafted `data_size` can make `extra_p` overshoot `p`, and the exit condition `while (extra_p != p)` is a pointer-equality check, so an overshoot causes the loop to keep scanning attacker-influenced memory outside the intended extra-field bounds, and `get_u8le` to read 8 bytes starting at an offset that may already be past the mapped file, before the fixed-size copy `memcpy(filename, p, len)` even executes with a length bounded by `filename_size` (`PATH_MAX`, from the `InputZipFile::filename` member) [6](#0-5) [7](#0-6) .

Because `zipdata_in_` and `central_dir_` point into a fixed-size `mmap`'d region sized by the actual file length, reading past the declared field boundaries with attacker-chosen offsets can walk the cursor past the end of that mapped region, producing an out-of-bounds read (and potential SIGSEGV/info disclosure into `filename`/size fields consumed by the caller).

### Impact Explanation
This is a memory-safety bug (CWE-125 style out-of-bounds read) in a C++ parser that consumes fully attacker-controlled bytes (a ZIP/JAR's central directory), analogous to the PJSIP SDP/RTP parser buffer overflow in the CVE report: insufficient bounds validation of length fields taken directly from untrusted wire/file data. Depending on how far the cursor is walked past the mapping, this can crash the ijar toolchain process (denial of service on the build) or leak adjacent process memory into fields (`filename`, `uncompressed_size`) that are subsequently used by the caller/consumer.

### Likelihood Explanation
`ZipExtractor`/`InputZipFile` in `third_party/ijar/zip.cc` is the parser used by the `ijar` interface-jar generation tool [8](#0-7) , which Bazel invokes on Java jar artifacts as part of normal Java build actions. An attacker who controls the content of a jar consumed by a Bazel build (e.g., published at a URL fetched via `http_archive`/`http_file`, or as a Maven artifact) fully controls every central-directory byte parsed by this code, satisfying the "unprivileged attacker publishing content the victim's build consumes" threat model. A correct `sha256`/`integrity` pin on the download does not stop this: the checksum verifies the exact attacker-chosen bytes were downloaded — it does not (and cannot) validate that those bytes are a well-formed, safely-parseable ZIP structure.

### Recommendation
Add `EnsureRemaining()` (or equivalent length checks against `input_file_->Length()`) inside `InputZipFile::ProcessCentralDirEntry` before consuming `file_name_length`, `extra_field_length`, `file_comment_length` bytes, and before each ZIP64 extra-field sub-record read, verifying `data_size >= 8` and that `extra_p + data_size` stays within `p` (the declared end of the extra field) and within the mapped file bounds.

### Proof of Concept
A reproducible JUnit/C++ test can be added alongside the existing malformed-input tests (e.g. `src/tools/singlejar/input_jar_bad_jar_test.cc`, `third_party/ijar/zip_main.cc`'s `extract()` path) that constructs a minimal ZIP file whose central directory record declares `extra_field_length` referencing a ZIP64 extra field with `data_size` less than 8 but `header_id == ZIP64_EXTRA_FIELD_TAG`, or an `extra_field_length`/`file_name_length` sum that exceeds the actual remaining bytes in the mapped file, then invokes `ZipExtractor::Create()`/`ProcessAll()` on it and observes an out-of-bounds read (crash under ASan, or divergent parsed values) rather than a graceful `error(...)` return, contrasting with the bounded behavior guaranteed for local file headers. [9](#0-8)

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

**File:** third_party/ijar/zip.cc (L493-542)
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
```

**File:** third_party/ijar/zip.h (L140-141)
```text

  // Process the next files, returns false if the end of ZIP file has been
```

**File:** third_party/ijar/ijar.cc (L1-1)
```text
// Copyright 2015 The Bazel Authors. All rights reserved.
```
