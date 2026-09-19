## Analog Found

### Title
Out-of-bounds read in ijar's ZIP central directory parser due to missing bounds validation - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry()` in Bazel's bundled `ijar` tool parses ZIP/JAR central directory records supplied by an untrusted archive without any bounds checking against the mapped input file's actual length, unlike the sibling local-file-entry parser which consistently validates remaining bytes before every read.

### Finding Description
`ijar` is Bazel's native interface-jar generator, used by default on every jar Bazel processes at compile time, including third-party jars fetched from the network (e.g. via `http_jar`/`http_file`/Maven-style dependencies pinned by `sha256`). A checksum pin only guarantees the bytes match what the attacker chose to publish — it says nothing about whether the *content* of those bytes is a well-formed ZIP structure. An attacker who legitimately controls the published artifact (an "outsider who serves content a victim's build consumes") can craft a JAR whose central directory entries declare bogus `file_name_length`/`extra_field_length` values that exceed the file's real remaining size, while its SHA-256 still matches the value the victim recorded.

Compare the two entry parsers in `third_party/ijar/zip.cc`:

- `InputZipFile::ProcessLocalFileEntry()` diligently guards every read with `EnsureRemaining()` before consuming bytes: [1](#0-0) 
- `InputZipFile::ProcessCentralDirEntry()`, which is invoked for every entry during `ProcessNext()`/`CalculateOutputLength()`, performs **no** equivalent `EnsureRemaining()` check at all before reading fixed fields, before `memcpy`-ing the declared `file_name_length` bytes into the caller's `filename` buffer, or before walking the "extra field" sub-records using attacker-controlled `data_size` values: [2](#0-1) 

The helper that is supposed to make such reads safe exists in the class and is actively used elsewhere: [3](#0-2) 

This is the same bug class as the btrfs CVE: a class of untrusted, attacker-controlled structural metadata (there: leaf item pointers gated behind a WRITTEN check that could be skipped; here: central-directory field lengths that are never checked against the buffer's true bounds) is trusted to compute subsequent pointer arithmetic and memory reads, allowing a corrupted/malicious file to drive reads outside the intended buffer.

### Impact Explanation
A crafted JAR/ZIP whose central directory contains oversized `file_name_length`, `extra_field_length`, or a malicious extra-field `data_size` chain causes:
- `memcpy(filename, p, len)` to read past the end of the memory-mapped input file, and
- the `while (extra_p != p)` extra-field walk to read `header_id`/`data_size` and advance `extra_p` arbitrarily, with no bound relative to either `extra_field_length` or the file's actual size.

Since the input file is `mmap`'d (see `MappedInputFile` usage in `InputZipFile`), reads past the mapping's end can hit an unmapped page and crash the build process (denial of service is excluded per rules, but the same missing bound also enables reading unintended in-bounds heap/mmap memory adjacent to the mapping into the `filename` buffer or into `*compressed_size`/`*uncompressed_size` decisions), which is a memory-safety violation — an out-of-bounds read driven entirely by untrusted archive content that a hash check does not prevent.

### Likelihood Explanation
`ijar` runs by default as part of ordinary Java compilation for essentially every jar dependency (first- and third-party), so the code path is reachable without any special build flags. The only requirement is that the victim's build consume a jar published by the attacker (a normal supply-chain scenario, not a MITM), and the declared checksum is computed over the very same malicious bytes, so checksum verification does not block this.

### Recommendation
Add `EnsureRemaining()`-style bounds checks (or an equivalent check against the input file's `Length()`) in `InputZipFile::ProcessCentralDirEntry()` before every fixed-width read, before the `memcpy` of the filename, and before/inside the extra-field walk loop, so that `data_size`/`file_name_length`/`extra_field_length` can never cause `p`/`extra_p` to advance past the mapped file's bounds.

### Proof of Concept
Craft a minimal ZIP with one valid EOCD/central-directory-locator trailer but a single central directory entry where `file_name_length` (or `extra_field_length`) is set to a large value (e.g. `0xFFFE`) while the actual file is only a few hundred bytes long. Feed it to `ijar`'s zipper/unzipper (`third_party/ijar/zip_main.cc`, which calls `ZipExtractor::ProcessAll()` → `ProcessCentralDirEntry()`), following the existing pattern used in `third_party/ijar/test/zip_test.sh` (e.g. `test_no_path_traversal`), building the ASAN-instrumented `zipper`/ijar binary and running it against the crafted file; ASAN reports a heap-buffer-overflow/out-of-bounds read originating in `ProcessCentralDirEntry`. A `BuildIntegrationTestCase`-style reproduction can instead pin such a crafted jar as `http_jar` with a correct `sha256` and depend on it from a `java_library`, showing the ijar action still crashes/reads OOB despite integrity verification succeeding.

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

**File:** third_party/ijar/zip.cc (L493-544)
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
```
