Found a genuine analog: `InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` parses the ZIP central directory (which feeds `ijar`/`singlejar` style archive processing used for `http_archive`/`http_jar` extraction and interface-jar generation) with **no bounds checking against the mapped file length** at any point, unlike `ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before every field read.

### Title
Unchecked buffer over-read while parsing attacker-controlled ZIP central directory entries - (File: third_party/ijar/zip.cc)

### Summary
`ProcessCentralDirEntry` reads the ZIP signature, sizes, filename length, extra-field length, comment length, attributes, and offset directly from the mmap'd input with `get_u4le`/`get_u2le`, then does `memcpy(filename, p, len)` and advances `p` by `file_name_length`, `extra_field_length`, and `file_comment_length` taken verbatim from the (attacker-supplied) archive bytes — with zero calls to `EnsureRemaining` (the bounds-check helper used everywhere else in this file, e.g. in `ProcessLocalFileEntry` at lines 334-370).

### Finding Description
`EnsureRemaining` in `InputZipFile` computes `remaining = input_file_->Length() - in_offset` and errors out if a requested field size would exceed the mapped file. `ProcessLocalFileEntry` [1](#0-0)  uses this consistently before reading `file_name_length_`/`extra_field_length_` bytes. In contrast, `ProcessCentralDirEntry` [2](#0-1)  never calls `EnsureRemaining` at all: it reads `file_name_length`, `extra_field_length`, `file_comment_length` from attacker-controlled bytes and then does `memcpy(filename, p, len)` (bounded only by the local stack buffer, not by remaining mmap length) at line 520, and unconditionally advances `p` past `file_name_length`, `extra_field_length` (with an inner extra-field-tag loop at lines 526-542 that trusts `data_size` similarly), and `file_comment_length` — all without verifying these lengths are within the bounds of the memory-mapped archive.

If a hostile origin serves a truncated or crafted ZIP/JAR (consumed via `http_archive`/`http_jar` or processed by `ijar`/`singlejar` during the build), a central-directory entry near the end of the mapped file with an inflated `file_name_length`/`extra_field_length`/`file_comment_length` causes `memcpy` and pointer arithmetic to read past the end of the mmap'd region — a buffer over-read, directly analogous to the ZIPEncode over-read in CVE-2016-3620, which likewise stemmed from trusting attacker-controlled size fields during archive/format processing without bounds validation.

The code comment at line 491-492 ("Note that the central directory is always followed by another data structure that has a signature, so parsing it this way is safe.") is the stated (but incorrect) justification for omitting bounds checks — it assumes a well-formed trailing EOCD record, which a hostile archive is not obligated to provide.

### Impact Explanation
This is a memory-safety violation (heap/mmap buffer over-read) in an unprivileged, remotely-triggerable code path: any content an attacker controls (a hostile mirror/origin serving a crafted `.zip`/`.jar`, or a malicious dependency artifact) that is unzipped/processed by `ijar` (used to build interface jars for `java_library`) or `singlejar`-adjacent zip-reading code can trigger a read outside the mapped archive buffer, potentially crashing the build (SIGSEGV) or leaking adjacent memory contents into filenames/output depending on what follows the mapping. It sits squarely in the "Extraction and patching" surface named in the prompt (ZipDecompressor/`zip.cc`'s `InputZipFile`).

### Likelihood Explanation
High: `ProcessCentralDirEntry` is invoked for every central directory record whenever `ijar` or any ZIP-based extraction/CalculateOutputLength (line 550-581) path processes an untrusted, attacker-served archive with default flags — no special configuration is required, and no existing checksum/integrity mechanism (sha256/integrity on `http_archive`) prevents the parser itself from over-reading a *malformed* file whose bytes still match the declared hash, since integrity checking validates the whole-file hash but not internal structural consistency before parsing.

### Recommendation
Add `EnsureRemaining()` (or equivalent bounds checks against `input_file_->Length()`) calls in `ProcessCentralDirEntry` before every advancement of `p` by `file_name_length`, `extra_field_length`, `file_comment_length`, and inside the extra-field loop before trusting `data_size`, mirroring the pattern already used in `ProcessLocalFileEntry`.

### Proof of Concept
A reproducible test would construct a minimal ZIP file whose central directory contains a single entry with `file_name_length` (or `extra_field_length`/`file_comment_length`) set to a value that extends past the actual end of the mapped file (e.g., no EOCD record follows, or the declared length exceeds remaining bytes), then invoke `ZipExtractor::Create()` / `ProcessAll()` (as done in `third_party/ijar/zip_main.cc`) on it under ASan; the expected assertion is an ASan heap/mmap-buffer-overflow report inside `ProcessCentralDirEntry`'s `memcpy` or pointer arithmetic, as opposed to the graceful `EnsureRemaining`-driven error seen for equivalent local-header malformations. I was not able to execute this PoC in this environment; the recommendation is to author it as a `third_party/ijar` unit test (or extend an existing test in that directory) that feeds a truncated/corrupted central directory to `ZipExtractor`. [2](#0-1) [3](#0-2) [4](#0-3)

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
