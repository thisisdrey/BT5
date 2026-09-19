### Title
Heap buffer over-read parsing untrusted ZIP/JAR central directory entries in ijar - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` reads variable-length central-directory fields (`compressed_size`, `uncompressed_size`, `file_name_length`, `extra_field_length`, `file_comment_length`, extra-field sub-records) directly from a memory-mapped, attacker-controlled ZIP/JAR file without any bounds check, unlike the sibling local-file-header parser which explicitly validates remaining bytes before every variable-length read.

### Finding Description
`InputZipFile::ProcessLocalFileEntry` calls `EnsureRemaining()` before reading `file_name_length_`/`extra_field_length_`-sized regions: [1](#0-0) 

By contrast, `InputZipFile::ProcessCentralDirEntry` — which walks the ZIP central directory entry by entry — performs no equivalent `EnsureRemaining` check. It reads the fixed header fields, then `file_name_length`, `extra_field_length`, `file_comment_length`, and advances `p` by these attacker-supplied 16-bit lengths, then walks an inner loop over "extra field" sub-records using `data_size` read directly from the buffer: [2](#0-1) 

The function's only safety argument is a comment: "Note that the central directory is always followed by another data structure that has a signature, so parsing it this way is safe" [3](#0-2) . This is an unverified invariant about attacker-controlled bytes, not an actual bounds check against `input_file_->Length()`. A crafted/truncated central directory record (e.g., an oversized `file_name_length`, `extra_field_length`, or an "extra field" sub-record whose `data_size` exceeds the actual remaining `extra_field_length`) causes `p`/`extra_p` to advance past the end of the mmap'd input file, and subsequent `get_u2le`/`get_u4le`/`get_u8le`/`memcpy` calls read out-of-bounds heap memory — the same bug class as CVE-2017-10989 (`getNodeSize` trusting undersized/crafted blob metadata without validating remaining buffer length before reading fixed-size fields from it).

This code path (`ijar`) is used by Bazel to strip interface jars from dependency `.jar` artifacts, which are untrusted content fetched from external repositories/registries (e.g. via `http_jar`/`http_archive`/Maven-style deps) — exactly the "unprivileged remote content" trust boundary in scope.

### Impact Explanation
Reading past the end of the memory-mapped input file is a heap-adjacent out-of-bounds read. Depending on subsequent use (e.g. `memcpy` into `filename[PATH_MAX]` with an unvalidated `len`, or interpreting stray heap bytes as ZIP64 extra-field values used later to size copies/offsets in `OutputZipFile`), this can crash the build (denial of service) or leak adjacent process memory into build output/errors. It does not directly grant write-outside-sandbox or credential exfiltration, but it is a concrete memory-safety violation triggered purely by attacker-controlled archive bytes, matching the CVE's "heap-based buffer over-read or possibly unspecified other impact."

### Likelihood Explanation
`ijar` runs on every jar processed by Bazel's Java/interface-jar tooling and mmaps input jars without further validating internal ZIP structure sizes against the file length in the central-directory path. Any JAR fetched from a hostile origin/mirror/registry (unpinned or checksum-verified-but-attacker-controlled-before-first-fetch, or an untrusted branch's generated jar) that a victim's build processes with `ijar` would trigger this path. No special build flags are needed since `EnsureRemaining` simply doesn't exist in this function.

### Recommendation
Add `EnsureRemaining`-style bounds checks in `InputZipFile::ProcessCentralDirEntry` before consuming `file_name_length`, `extra_field_length`, `file_comment_length`, and before reading each extra-field sub-record's `data_size` bytes, mirroring the checks already present in `ProcessLocalFileEntry`.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` style repro: construct a minimal ZIP whose central directory entry declares `extra_field_length` (or an embedded ZIP64 extra-field `data_size`) larger than the bytes actually present before EOF, memory-map it, and invoke `ijar`/`InputZipFile::ProcessNext()` on it (as the interface-jar-stripping tool does on fetched jars). Under ASan, the out-of-bounds `get_u2le`/`get_u8le`/`memcpy` reads in `ProcessCentralDirEntry` are flagged as heap-buffer-overflow (read).

**Note on limitations:** I was not able to fully view `InputZipFile::Open()`/central-directory-location code (EOCD search) within the remaining tool budget, so I cannot confirm whether an earlier check bounds `central_dir_`/`central_dir_current_` to the mapped file size before `ProcessCentralDirEntry` begins walking it entry-by-entry. If such a check exists, it would only guarantee the *start* of the central directory is in-bounds, not that every subsequent attacker-controlled length field stays in-bounds as `p`/`extra_p` are advanced — the missing `EnsureRemaining` calls shown above remain the concrete gap.

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

**File:** third_party/ijar/zip.cc (L491-492)
```text
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
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
