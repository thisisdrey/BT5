Based on my research, I found a concrete memory-bounds analog in Bazel's own C++ code (not a third-party dependency), matching the CVE-2023-28410 bug class of "improper restriction of operations within the bounds of a memory buffer."

### Title
Out-of-bounds read when parsing ZIP central directory entries in `ijar` - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in Bazel's `ijar` tool (used to strip non-ABI bytecode from `.jar` files that Bazel consumes as dependencies) reads variable-length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) from a memory-mapped ZIP/JAR file and advances/dereferences the cursor `p` using these attacker-controlled lengths without any bounds validation against the mapped buffer's end.

### Finding Description
`InputZipFile::ProcessLocalFileEntry` (local file header parsing) explicitly guards every variable-length read with `EnsureRemaining()`: [1](#0-0) 

By contrast, `ProcessCentralDirEntry`, which parses the central directory (the structure that drives all subsequent offsets/sizes used for extraction), performs no such check. It reads `compressed_size`, `uncompressed_size`, `file_name_length`, `extra_field_length`, `file_comment_length`, `attr`, and `offset` via raw `get_u4le`/`get_u2le` calls, then copies `file_name_length` bytes from `p` into a fixed `filename[PATH_MAX]` buffer, and walks an extra-field loop reading `header_id`/`data_size` and (for a Zip64 tag) `get_u8le(extra)`, again with no check that `p`, `extra_p`, or `extra` stay inside the mapped file: [2](#0-1) 

The only prior validation is that the central directory as a whole fits in the file (`cd.central_dir_offset + cd.central_dir_size <= in_length`) in `FindZipCentralDirectory`: [3](#0-2) 

That check bounds the *aggregate* central directory region, but nothing bounds an *individual* malformed entry's declared `file_name_length`/`extra_field_length`/`file_comment_length` against the remaining bytes actually available before the mapped region ends. A crafted JAR/ZIP with an oversized length field on the last (or a near-boundary) central directory entry causes `p`/`extra_p` to run past the end of the `mmap`'d file, and the subsequent `memcpy(filename, p, len)` (bounded only on the destination side to `PATH_MAX`) reads out-of-bounds source memory adjacent to the mapping.

### Impact Explanation
This is a genuine "read outside the bounds of a memory buffer" bug in Bazel's own native code, directly analogous to the reported driver CVE's bug class. The out-of-bounds bytes are copied into `filename`, which is subsequently used for `processor->Accept()`/`Process()` decisions and is embedded in the tool's diagnostics/processing pipeline — meaning adjacent process/heap memory contents from the `ijar` process can leak into filenames used to drive interface-jar generation, and in degenerate cases the mapped region boundary crossing can crash the process (deterministic on some platforms depending on page alignment).

### Likelihood Explanation
`ijar` runs unconditionally whenever Bazel needs to produce an interface jar from a `.jar` dependency (e.g. for `java_import`, prebuilt jars fetched via `http_jar`/`http_file`, or jars checked into an untrusted branch that CI builds). An attacker who controls the content served at a dependency URL or checked into a repo branch that gets built can supply a JAR whose central directory entry near the end of the file has an oversized `file_name_length`/`extra_field_length`, triggering the OOB read without any special privileges, matching the "unprivileged content publisher" attacker model.

### Recommendation
Add explicit bounds checks in `InputZipFile::ProcessCentralDirEntry` (mirroring `EnsureRemaining()` used in `ProcessLocalFileEntry`) before advancing `p` by `file_name_length`, `extra_field_length`, and `file_comment_length`, and before dereferencing the extra-field loop's `header_id`/`data_size`/Zip64 attribute reads, so that every read is validated against the actual end of the mapped input file rather than only against the aggregate central-directory size.

### Proof of Concept
A reproducible JUnit/`googletest` case (alongside the existing `src/tools/singlejar` tests that already build malformed-extra-field ZIPs, see `CreateZipWithMalformedExtraField` in `output_jar_simple_test.cc`) would:
1. Construct a minimal valid ZIP with a correct EOCD/central-directory size, but set the last central directory entry's `file_name_length` (or `extra_field_length`) to a value that extends past the actual end of the buffer/mapped file.
2. Invoke `ijar`'s extraction/central-directory-scanning path (`InputZipFile::ProcessNext` / `CalculateOutputLength`) on this file.
3. Observe an out-of-bounds read (e.g., via ASan `heap-buffer-overflow`/`SEGV` on a page boundary, or via reading uninitialized/adjacent bytes into `filename`). [4](#0-3) [5](#0-4)

### Citations

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

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```
