### Title
Out-of-bounds read in ijar's ZIP central-directory parser due to missing bounds checks - ([File: third_party/ijar/zip.cc])

### Finding Description
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` parses a Central Directory Header (CDH) entry directly from the mmapped, attacker-controlled ZIP/JAR bytes without ever validating that the declared lengths fit within the mapped file. [1](#0-0) 

After reading `compressed_size`, `uncompressed_size`, `file_name_length`, `extra_field_length`, and `file_comment_length` straight from attacker-supplied bytes, the function immediately does:
```
memcpy(reinterpret_cast<void*>(filename), p, len);
```
using `p` and a length derived from `file_name_length`, and then advances `p` by `file_name_length` and `extra_field_length` with no check that these advances stay inside the mapped input file. [2](#0-1) 

The subsequent extra-field walking loop is even more clearly unguarded: it reads a `header_id`/`data_size` pair via `get_u2le(extra_p)` and advances `extra_p += data_size`, looping `while (extra_p != p)`. There is no check that `extra_p + 4 <= p` before reading the header/size fields, and no check that `data_size` keeps `extra_p` from overshooting `p`. If a malicious central-directory entry declares an `extra_field_length`/`data_size` combination that never lands `extra_p` exactly on `p` (e.g., an odd/miscomputed size), the loop keeps reading 2-byte little-endian values past the intended extra-field region — and potentially past the end of the mmapped file — until it happens to satisfy the equality or crashes.

This is in stark contrast to the sibling function `InputZipFile::ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining(...)` before every variable-length read: [3](#0-2) 

`ProcessCentralDirEntry` has no equivalent `EnsureRemaining` calls at all, i.e., the missing-bounds-check pattern that defines this CVE class.

### Impact Explanation
ijar is Bazel's native tool for generating interface jars, and it consumes JAR/ZIP files that can originate from externally fetched, attacker-influenced artifacts (e.g., prebuilt jars pulled in via `http_jar`/`http_archive` and passed through `java_import`/ijar-based actions). Because the central-directory parser trusts length fields taken directly from the archive without bounds validation, a maliciously crafted archive can cause the parser to read memory beyond the mapped file buffer (heap/mmap out-of-bounds read). This can leak adjacent process memory contents into build outputs/diagnostics or crash the process — an information-disclosure/DoS primitive matching the "missing bounds check → OOB read" class described in the report, reachable purely from untrusted archive bytes with no attacker access to the victim machine.

### Likelihood Explanation
Any attacker who controls the origin/mirror serving a `.jar`/`.zip` consumed by a Bazel build (and whose sha256, if declared, is against attacker-chosen bytes) can trivially craft a central directory entry with an inconsistent `extra_field_length`/`data_size` or oversized `file_name_length` relative to the actual (possibly truncated) mapped file size. No user interaction beyond a normal build/fetch is required, matching the "no user interaction, local execution privileges" profile of the analog CVE.

### Recommendation
Add the same `EnsureRemaining()`-style bounds validation used in `ProcessLocalFileEntry` to `ProcessCentralDirEntry`: verify remaining bytes before reading `file_name_length`, `extra_field_length`, `file_comment_length` bytes, and inside the extra-field loop verify `extra_p + 4 <= p` before reading `header_id`/`data_size`, and that `extra_p + data_size <= p` before advancing, aborting with an error instead of continuing to read.

### Proof of Concept
Construct a ZIP file whose Central Directory Header declares `extra_field_length` that does not evenly divide into consistent `(header_id, data_size)` pairs (e.g., a single 3-byte "leftover" tail) or whose actual mapped-file length is shorter than `file_name_length` implies (truncated file). Feeding this into ijar (`third_party/ijar/zip_main.cc` extract mode, or via a `java_import`/interface-jar-generating action consuming a downloaded jar) drives `ProcessCentralDirEntry`'s extra-field loop past the intended region / drives the filename `memcpy` past the mapped file end, producing an out-of-bounds read that can be observed via a crash (ASan heap-buffer-overflow) or leaked bytes copied into `filename`/emitted output. A JUnit/`BuildIntegrationTestCase` equivalent would build a minimal crafted ZIP with a mismatched `extra_field_length` (as in `src/tools/singlejar/zip_headers_test.cc`'s style of crafting header bytes) and assert that `ProcessCentralDirEntry` returns an error instead of reading past the buffer, ideally run under AddressSanitizer to detect the OOB access directly.

### Citations

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

**File:** third_party/ijar/zip.cc (L524-543)
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
  p += file_comment_length;
```
