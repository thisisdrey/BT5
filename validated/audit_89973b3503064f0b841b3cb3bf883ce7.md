This confirms the key asymmetry: `ProcessLocalFileEntry` (local header parsing) carefully calls `EnsureRemaining()` before every pointer advance [1](#0-0) , but `ProcessCentralDirEntry` (central directory parsing) performs no such bounds checks before advancing `p` past `file_name_length`, `extra_field_length`, and the nested extra-field records [2](#0-1) , relying only on the comment's unverified assumption that "the central directory is always followed by another data structure that has a signature, so parsing it this way is safe" [3](#0-2) .

### Title
Heap-based buffer over-read in ijar's ZIP central-directory extra-field parser - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessCentralDirEntry` in ijar's ZIP reader parses central-directory-header (CDH) fields and the extra-field record chain that follows the filename with no bounds checking against the actual mapped file length, unlike its sibling function for local file headers.

### Finding Description
`ProcessCentralDirEntry` reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly from attacker-controlled bytes in a ZIP/JAR file, then blindly advances the cursor `p` by these attacker-chosen 16-bit values with no check that they stay within the mapped input buffer [2](#0-1) . It then walks the extra-field records with `while (extra_p != p)`, reading a 2-byte `header_id` and 2-byte `data_size` at each step via `get_u2le` and advancing `extra_p += data_size` without ever verifying `extra_p + 4 <= p` before reading the header, or that `data_size` doesn't push `extra_p` past `p` [4](#0-3) . If `data_size` is crafted so that `extra_p` overshoots the true end of the extra-field area (`p`), the loop condition `extra_p != p` never becomes false, and the loop continues reading 2-, 4-, and 8-byte little-endian values (`get_u2le`/`get_u4le`/`get_u8le`) from memory beyond the extra-field region — and potentially beyond the entire mapped buffer — until it happens to land exactly on the original `p` value again (which, for attacker-chosen offsets, may never occur within the mapped region), continuing to walk arbitrary heap memory following the buffer. This is structurally the same bug class as CVE-2019-17594: a length/index derived from untrusted structured data is used to advance a read cursor without validating it against the buffer's true bounds, resulting in reads past the intended data structure into adjacent heap memory. Contrast this with `ProcessLocalFileEntry`, which guards every analogous advance with `EnsureRemaining()` [5](#0-4) , showing the central-directory path is the outlier lacking this defense.

### Impact Explanation
A malicious or corrupted JAR/ZIP archive processed by ijar (used by Bazel to build interface jars, e.g., for `java_import`/`java_library` and jars obtained via `http_jar`/`http_archive`/Maven-style external dependencies) can trigger reads past the mapped input buffer. Depending on heap/mmap layout this manifests as a crash (denial of service to the single build invocation) or, since the read values (`uncompressed_size`, `compressed_size`, `offset`) feed into subsequent size/offset computations, potential further memory-safety issues downstream. This is a memory-safety defect in native code processing untrusted archive bytes, matching the "heap-based buffer over-read" class of the reference CVE.

### Likelihood Explanation
Any archive an outsider can serve (e.g., a jar file at a URL fetched via `http_jar`/`http_archive`, or embedded in a package the victim's build consumes) can be crafted with a pathological extra-field/CDH byte sequence; the JAR's own SHA-256/integrity check (if declared) is computed over the exact malicious bytes chosen by the attacker, so a correctly pinned checksum does not prevent this — the attacker simply publishes the bytes and lets the victim pin them, exactly as with the ncurses/terminfo analog where a hostile database entry (not integrity bypass) triggers the bug.

### Recommendation
Add explicit bounds checks in `ProcessCentralDirEntry` mirroring `EnsureRemaining()` in `ProcessLocalFileEntry`: validate `file_name_length`, `extra_field_length`, and `file_comment_length` against the number of bytes actually remaining in the mapped central directory/buffer before advancing `p`, and validate each extra-field record's `header_id`/`data_size` read is within `[extra_p, p)` before dereferencing, rejecting the archive with an error instead of reading out of bounds.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell` reproduction would construct a minimal valid ZIP with a correct End-of-Central-Directory record but a Central Directory Header entry whose `extra_field_length` is small (e.g., 4) while the embedded extra-field record declares a `data_size` far exceeding the remaining central-directory bytes (e.g., 0xFFFF), placed near the end of the mapped file so that `extra_p` walks past the buffer end; running ijar (or a `java_import`/interface-jar-generation rule) on this archive should be shown to crash or read invalid memory under AddressSanitizer, whereas a well-formed archive of identical size processes cleanly.

### Citations

**File:** third_party/ijar/zip.cc (L334-370)
```text
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

**File:** third_party/ijar/zip.cc (L486-493)
```text
// - whether the entry is a class file (to be included in the output).
// Precondition: p points to the beginning of an entry in the central dir
// Postcondition: p points to the beginning of the next entry in the central dir
// Returns true if the central directory contains another file and false if not.
// Of course, in the latter case, the size output variables are not changed.
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
bool InputZipFile::ProcessCentralDirEntry(const u1 *&p, u8 *compressed_size,
```

**File:** third_party/ijar/zip.cc (L507-542)
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
