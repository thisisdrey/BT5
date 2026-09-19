### Title
Heap buffer over-read in `InputZipFile::ProcessCentralDirEntry` extra-field loop due to unchecked pointer bounds - (File: `third_party/ijar/zip.cc`)

### Summary
`ProcessCentralDirEntry` in ijar's ZIP central-directory parser reads a ZIP64 extra-field sub-record (`header_id`, `data_size`) and, if `header_id == ZIP64_EXTRA_FIELD_TAG`, calls `get_u8le(extra)` up to three times without ever verifying that `extra_field_length`/`data_size` actually cover 8/16/24 bytes, nor that the resulting reads stay within the mapped file. This mirrors the libdwarf `_dwarf_read_loc_expr_op()` bug class: a parser advances a cursor through attacker-supplied sub-records and dereferences memory beyond the record's declared bounds because a pointer is never checked against the buffer's end.

### Finding Description
`InputZipFile::ProcessCentralDirEntry` [1](#0-0)  parses one Central Directory Header (CDH) entry from a memory-mapped, attacker-controlled ZIP file. After computing `file_name_length` and `extra_field_length` from attacker-controlled 16-bit fields, it sets `extra_p = p` and `p += extra_field_length`, then loops:

```
while (extra_p != p) {
  const u2 header_id = get_u2le(extra_p);
  const u2 data_size = get_u2le(extra_p);
  const u1 *extra = extra_p;
  extra_p += data_size;
  if (header_id == ZIP64_EXTRA_FIELD_TAG) {
    if (*uncompressed_size == U4_MAX) *uncompressed_size = get_u8le(extra);
    if (*compressed_size == U4_MAX)   *compressed_size   = get_u8le(extra);
    if (*offset == U4_MAX)            *offset            = get_u8le(extra);
  }
}
``` [2](#0-1) 

`get_u8le` advances the read pointer by 4 bytes twice (via `get_u4le`) [3](#0-2) . If an attacker crafts a ZIP64 extra field whose declared `data_size` is small (e.g., 0, or less than 8/16/24) but whose `header_id` equals `ZIP64_EXTRA_FIELD_TAG` (0x0001), the code will still call `get_u8le(extra)` up to three times, reading up to 24 bytes from `extra` regardless of the actual sub-record size. This reads past the extra-field region — and potentially past `extra_field_length`, past the CDH entry, and (for an entry placed at the very end of the mapped file) past the end of the `mmap`'d region — because there is no check that `data_size >= 8/16/24` nor that `extra + 24 <= p` (end of extra-field area) before dereferencing. This directly parallels the analog's required class: "Extraction and patching" parsers (`ZipDecompressor`-class code) failing to keep a cursor within bounds while walking attacker-supplied sub-records, exactly as in `_dwarf_read_loc_expr_op()`.

Unlike `ProcessLocalFileEntry`, which calls `EnsureRemaining()` before every pointer advance to prevent reading past the mapped file [4](#0-3) , `ProcessCentralDirEntry` has no equivalent bounds-check calls at all — none of `EnsureRemaining` is invoked anywhere in this function, and the local `extra_p`/`p` cursor advancement is trusted implicitly based on the comment "Note that the central directory is always followed by another data structure that has a signature, so parsing it this way is safe" [5](#0-4) . That invariant only bounds the *outer* entry-to-entry traversal; it says nothing about the *inner* extra-field sub-record parsing, which is the actual over-read surface.

### Impact Explanation
This is a heap (mmap) buffer over-read: `ijar`/`zip.cc` reads up to a few dozen bytes past the intended sub-record boundary of a memory-mapped file. Depending on layout, this could read past the end of the `mmap`'d region into unmapped memory (causing a crash/SIGSEGV, i.e., availability impact against the `ijar` tool invoked as part of Bazel's Java compilation pipeline) or read adjacent heap memory that ends up incorporated into `*uncompressed_size`, `*compressed_size`, or `*offset`, corrupting subsequent size/offset computations used to build the interface jar output. There is no confidentiality exfiltration path (values are only used internally for size accounting), so impact is best characterized as a crash/DoS or corrupted-size-driven miscomputation within the `ijar` process, not remote code execution.

### Likelihood Explanation
Exploitability requires an attacker to supply a crafted ZIP/JAR file that is subsequently processed by `ijar` (used by Bazel to generate interface jars for Java compilation from `.jar` inputs). This is plausible in a "hostile origin server" scenario where a `java_import`/dependency jar is fetched from an untrusted URL and — critically — the checksum verification for such an artifact would need to fail to stop it, OR the artifact could be built from an untrusted branch's jar deps that go through `ijar` without any checksum ever being validated. Whether `ijar`'s CDH parsing path is reachable purely from downloaded/attacker-influenced jars before checksum validation, and whether it is reachable with default flags, could not be fully confirmed in this review — I was unable to trace the exact call path from `http_jar`/`http_archive` fetch verification into `ijar` invocation within the available context, so likelihood should be considered moderate/uncertain pending a deeper trace of the build-time jar-consumption pipeline.

### Recommendation
Add explicit bounds checks in the extra-field loop of `ProcessCentralDirEntry`: verify `data_size` is at least 8 (and 16/24 for the additional optional fields) before calling `get_u8le`, and verify `extra_p <= p` (the extra-field region end) and that `extra + required_bytes` does not exceed `p` before each `get_u8le` call. Additionally, add an `EnsureRemaining`-style check anchored to the mapped file's total length so a malformed `extra_field_length` cannot cause `p`/`extra_p` to walk past `input_file_->Length()`.

### Proof of Concept
A JUnit/C++ gtest-style reproduction would construct an in-memory ZIP whose Central Directory Header for one entry has: `extra_field_length = 4` (i.e., only 4 bytes of extra-field payload, enough for `header_id`+`data_size` but not enough for any ZIP64 attribute value), with `header_id = 0x0001` (ZIP64_EXTRA_FIELD_TAG) and `data_size = 0`, and either `uncompressed_file_size32` or `compressed_file_size32` set to `0xFFFFFFFF` (triggering the ZIP64 read path). The CDH entry should be placed at (or near) the very end of the mmap'd buffer so that reading 8 extra bytes runs past the mapped region, and the test should assert that `InputZipFile::ProcessCentralDirEntry` performs an out-of-bounds read (detectable via ASan/heap-buffer-overflow instrumentation) rather than validating `data_size` first.

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

**File:** third_party/ijar/common.h (L55-72)
```text
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

inline u8 get_u8le(const u1 *&p) {
  u4 lo = get_u4le(p);
  u4 hi = get_u4le(p);
  u8 x = ((u8)hi << 32) | lo;
  return x;
}
```
