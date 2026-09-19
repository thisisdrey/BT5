### Title
Out-of-Bound Read in `InputZipFile::ProcessCentralDirEntry` when parsing attacker-supplied ZIP central directory (`third_party/ijar/zip.cc`)

### Summary
The ijar ZIP parser (used to build interface jars, and reachable via jar/zip handling in Bazel's build tooling) parses the ZIP central directory in `InputZipFile::ProcessCentralDirEntry` by unconditionally reading fixed-size fields, filename, extra-field, and comment lengths, and then advancing `p` by attacker-controlled length values (`file_name_length`, `extra_field_length`, `file_comment_length`) with no bounds check against the buffer's actual size before each read.

### Finding Description
`ProcessCentralDirEntry` reads the 4-byte signature, then unconditionally skips 16 bytes and calls `get_u4le`/`get_u2le` repeatedly to extract `compressed_size`, `uncompressed_size`, `file_name_length`, `extra_field_length`, `file_comment_length`, `attr`, and `offset`, none of which are preceded by an `EnsureRemaining()` bounds check [1](#0-0) . It then advances `p` by `file_name_length` and copies from `p` via `memcpy` into a fixed `filename` buffer sized by `filename_size`, again without validating that `file_name_length` bytes actually exist in the mapped input before the copy [2](#0-1) . The subsequent extra-field loop advances `extra_p` by `data_size` values read directly from the buffer and dereferences `get_u8le(extra)` for zip64 fields without checking that `extra_p + data_size` (or the 64-bit read) stays within the extra-field region [3](#0-2) . Finally `p += file_comment_length` advances past the entry with no bounds validation [4](#0-3) .

By contrast, other code paths in the same file (`SkipFile`, `ProcessFile`) explicitly call `EnsureRemaining()` before reading file data [5](#0-4) [6](#0-5) , showing that the codebase treats `EnsureRemaining` as the standard integrity boundary for reads from attacker-controlled zip content, but this boundary is absent in the central-directory entry parser. The code comment at the top of the function even claims "the central directory is always followed by another data structure that has a signature, so parsing it this way is safe" [7](#0-6) , an assumption that a crafted, truncated, or size-inconsistent ZIP (e.g., where `file_name_length`/`extra_field_length`/`file_comment_length` fields point past the actual mapped file length, or where the central directory is the last bytes in the file) can violate, causing reads past the end of the mapped input buffer.

### Impact Explanation
This is a direct analog to the libxrdp `libxrdp_send_to_channel()` OOB read: attacker-controlled length fields drive pointer arithmetic and memory reads without validating remaining buffer size, in a component that processes untrusted external data (a downloaded/fetched jar or zip file). Depending on how the input file is mapped (e.g., `mmap`-backed `InputFile` implementations used by ijar), an out-of-bounds read can cause a crash (segfault reading past a page boundary) or leak adjacent heap/mapped memory bytes into the output artifact (e.g., via the `memcpy` of `filename`, or by treating an out-of-range comment/extra field as valid zip64 size/offset data). This qualifies as a concrete out-of-bounds read primitive on attacker-published archive content.

### Likelihood Explanation
ijar processes jars/zips that could originate from externally-fetched dependencies (e.g., built by `http_archive`, `http_jar` or similar rules, or supplied as build inputs) — content an unprivileged attacker can control by serving a malicious archive at a URL a victim's build fetches. No credentials, MITM, or local machine access are required; a hostile origin server providing a specially-crafted (truncated or size-manipulated) ZIP central directory is sufficient to trigger the missing bounds checks. This is not blocked by SHA-256/integrity checks on the outer HTTP fetch, since those checks validate the *whole downloaded file's* hash, not the internal consistency of ZIP structural fields — a malicious archive can still have a valid overall hash matching what's pinned (if the attacker controls what's served/pinned, e.g., a first-time fetch or an archive not yet pinned) while containing corrupted/inconsistent internal length fields.

### Recommendation
Add explicit bounds checks (using the existing `EnsureRemaining()` helper or an equivalent buffer-boundary check relative to `central_dir_`/`central_dir_size_`) before every read and pointer-advance operation in `ProcessCentralDirEntry`, including the fixed header fields, the filename copy, the extra-field loop's `header_id`/`data_size` reads and `extra_p += data_size` advances, and the trailing `file_comment_length` skip. Ensure the zip64 extra-field parsing validates `data_size` against the declared/available extra-field length before performing 8-byte reads.

### Proof of Concept
A reproducible JUnit/`BuildIntegrationTestCase`-style proof would construct a ZIP file whose central directory entry declares a `file_name_length`, `extra_field_length`, or `file_comment_length` that extends beyond the actual byte length of the mapped input file (e.g., a truncated file ending right after the central directory header's fixed fields, with a large `file_name_length`), then invoke ijar's zip-processing entry point (e.g., through the `ijar`/ zipper tool or a Bazel action that consumes such a jar) and observe a crash or out-of-bounds read rather than a clean parse error. Concretely reproducing and verifying the exact crash characteristics (mmap vs. heap-backed input, ASAN detection) requires running the ijar binary against such a crafted file, which I was not able to execute in this read-only investigation — this should be validated by a background agent with build/test execution access.

### Citations

**File:** third_party/ijar/zip.cc (L430-432)
```text
  if (EnsureRemaining(compressed_size_, "file_data") < 0) {
    return -1;
  }
```

**File:** third_party/ijar/zip.cc (L472-474)
```text
    if (EnsureRemaining(compressed_size_, "file_data") < 0) {
      return -1;
    }
```

**File:** third_party/ijar/zip.cc (L486-492)
```text
// - whether the entry is a class file (to be included in the output).
// Precondition: p points to the beginning of an entry in the central dir
// Postcondition: p points to the beginning of the next entry in the central dir
// Returns true if the central directory contains another file and false if not.
// Of course, in the latter case, the size output variables are not changed.
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
```

**File:** third_party/ijar/zip.cc (L493-523)
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

**File:** third_party/ijar/zip.cc (L543-544)
```text
  p += file_comment_length;
  return true;
```
