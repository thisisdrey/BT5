### Title
Heap buffer over-read parsing an attacker-crafted ZIP/JAR central directory in `InputZipFile::ProcessCentralDirEntry` - (File: `third_party/ijar/zip.cc`)

### Summary
`ProcessCentralDirEntry` in Bazel's bundled `ijar` ZIP reader parses the central-directory record of a ZIP/JAR file entirely without any remaining-bytes validation, unlike its sibling `ProcessLocalFileEntry`, which calls `EnsureRemaining()` before every read. This class of bug — reading structured, attacker-controlled header/length fields straight out of a memory-mapped buffer with no bounds check before decoding image/archive-format metadata — is the same bug class as CVE-2019-19953 (GraphicsMagick `pict.c` `EncodeImage` heap over-read from unvalidated length fields in image metadata).

### Finding Description
`get_u4le`/`get_u2le` in `third_party/ijar/common.h` are raw pointer-dereferencing helpers with zero bounds checking [1](#0-0) . `ProcessLocalFileEntry` guards every multi-byte read behind `EnsureRemaining()`, which compares the requested size against `input_file_->Length() - in_offset` [2](#0-1) [3](#0-2) .

`ProcessCentralDirEntry`, however, reads the signature, sizes, `file_name_length`, `extra_field_length`, `file_comment_length`, `attr`, and `offset` fields, then `memcpy`s `file_name_length` bytes and walks an "extra field" loop driven entirely by attacker-supplied `data_size`/`header_id` values — with no call to `EnsureRemaining` or any check that `p`/`extra_p` stay within the mapped input buffer [4](#0-3) . The comment above the function ("the central directory is always followed by another data structure that has a signature, so parsing it this way is safe") is the only justification, and it does not hold once `file_name_length`, `extra_field_length`, or the zip64 extra-field `data_size` exceed the actual remaining bytes in the mmap'd file — the reads and the `memcpy` will run past the end of the mapped file/heap allocation.

This is reachable because `ijar`'s ZIP reader (`InputZipFile`) is used by Bazel's `ijar` tool to build interface jars from Java dependencies, which are fetched from attacker-influenced sources (e.g., an `http_archive`/`http_jar` whose `sha256`/`integrity` the attacker can still satisfy while crafting a malformed but hash-matching JAR, or a compromised build tool jar consumed without a pinned hash). The archive bytes are entirely attacker-controlled content parsed by native (unmanaged) C++ code operating on a raw mmap'd buffer, so any over-read is a genuine memory-safety violation, not merely a logic error.

### Impact Explanation
An out-of-bounds read in the ijar zip parser can crash the Bazel-invoked `ijar` binary (denial of service for the build), or in principle leak adjacent heap/mmap memory into a decoded field (e.g., `filename`) that later gets written into build output or an error message, causing information disclosure. Because `ijar` is a native binary run as part of ordinary Java compilation actions, a crafted JAR dependency can affect any build that depends on it.

### Likelihood Explanation
Likelihood is constrained by the requirement that the attacker supply the ZIP/JAR bytes that are consumed by `ijar` (e.g., as a `java_import`/`http_jar` dependency, or a jar produced by an untrusted build step) and that no other file-format validation upstream already truncates/rejects malformed entries. Because `ProcessCentralDirEntry` is invoked in a loop directly against a memory-mapped file with attacker-controlled length fields and no bounds checks, triggering an over-read only requires crafting a ZIP whose central directory declares lengths (`file_name_length`, `extra_field_length`, zip64 `data_size`) that exceed the actual remaining file size — a low-effort manipulation of an otherwise-valid ZIP structure.

### Recommendation
Add the same `EnsureRemaining()`-style bounds validation used in `ProcessLocalFileEntry` to `ProcessCentralDirEntry`: before reading the fixed 46-byte central directory header, and before the `memcpy` of `file_name_length` bytes, before advancing over `extra_field_length` bytes, and inside the zip64 extra-field loop before trusting `data_size`. Reject the archive with an error rather than reading past `input_file_->Length()`.

### Proof of Concept
A reproducible test would need to be added under `third_party/ijar/`, e.g. a unit test that:
1. Constructs a minimal ZIP with a valid End-Of-Central-Directory record but a single central directory entry whose `file_name_length` (or `extra_field_length`) field is set to a value larger than the number of bytes actually remaining in the buffer before EOCD.
2. Invokes `ZipExtractor::Create()` / `ProcessAll()` on this buffer (via `InputZipFile::ProcessCentralDirEntry`, exercised indirectly through `ProcessNext()`).
3. Runs under AddressSanitizer/Valgrind and observes a heap-buffer-overflow (read) report from the `memcpy(filename, p, len)` call or from the `get_u2le/get_u4le` calls in the extra-field loop, analogous to the `EncodeImage`/`pict.c` over-read in CVE-2019-19953.

Note: I was not able to execute this PoC or confirm via build/test tooling that no other layer (e.g., an earlier full-file CRC/EOCD sanity pass) prevents reaching this code path with out-of-range lengths — this would need to be verified with an actual ASan-instrumented run of `ijar`, which is outside the scope of static code reading in this session.

### Citations

**File:** third_party/ijar/common.h (L49-65)
```text
inline u2 get_u2le(const u1 *&p) {
    u4 x = (p[1] << 8) | p[0];
    p += 2;
    return x;
}

inline u4 get_u4be(const u1 *&p) {
    u4 x = (p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3];
    p += 4;
    return x;
}

inline u4 get_u4le(const u1 *&p) {
    u4 x = (p[3] << 24) | (p[2] << 16) | (p[1] << 8) | p[0];
    p += 4;
    return x;
}
```

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
