Confirmed: `run_ijar`/`java_common.run_ijar` runs the ijar C++ tool on arbitrary jars (e.g., a `.jar` provided via `java_import`/`http_jar`, whose bytes only need to satisfy a declared `sha256`/`integrity`), and internally it calls `InputZipFile::ProcessCentralDirEntry`, which performs no bounds checking before dereferencing memory-mapped file contents. [1](#0-0) 

### Title
Out-of-bounds read in ijar's ZIP central-directory parser processing an attacker-crafted `.jar` — (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in ijar (the tool invoked by `java_common.run_ijar`/`stamp_jar` to strip method bodies from a compile-time jar) reads the central directory header fields (`compressed_size`, `uncompressed_size`, `file_name_length`, `extra_field_length`, `file_comment_length`, `attr`, `offset`) and then `memcpy`s `file_name_length` bytes into a caller-owned buffer directly from mapped file memory, with zero bounds checking against the mapped file's actual length.

### Finding Description
`ProcessCentralDirEntry` reads the signature via `get_u4le(p)`, and if it matches `CENTRAL_FILE_HEADER_SIGNATURE`, unconditionally advances `p` by 16 bytes and continues to read further 2/4-byte fields and then does:
```
memcpy(reinterpret_cast<void*>(filename), p, len);
```
where `len` is derived from the attacker-controlled `file_name_length` field, and `p` points into the memory-mapped input file [2](#0-1) . Unlike `ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before every read [3](#0-2) , `ProcessCentralDirEntry` never checks that `p`, `p + file_name_length`, or `p + extra_field_length` stay within `input_file_->Length()`. A crafted `.jar`/`.zip` whose central directory record is placed near the end of the mapped region and declares an inflated `file_name_length`/`extra_field_length` causes reads past the end of the `mmap`'d file into adjacent memory.

This function is reached from `InputZipFile::ProcessNext()` (used while iterating every entry of the jar) and `InputZipFile::CalculateOutputLength()`, both of which are exercised whenever ijar processes an input jar [4](#0-3) . ijar is directly reachable from Starlark via `java_common.run_ijar(jar=..., java_toolchain=...)`, which is documented to run on a user-supplied `jar` File, e.g. one obtained from a `java_import`/`http_jar` external dependency.

### Impact Explanation
The declared `sha256`/`integrity` on an `http_jar`/`http_archive` only pins the raw bytes the attacker chooses to serve; it does not validate that the ZIP structure is well-formed. A malicious mirror or registry can therefore publish a `.jar` file whose bytes match a self-computed, valid checksum but whose internal ZIP central directory is deliberately malformed to trigger the out-of-bounds read once Bazel invokes `run_ijar` on it. Depending on heap layout this can leak adjacent process memory into the resulting interface jar's filename bytes (info disclosure) or crash the ijar subprocess (unhandled SIGSEGV), impacting the build.

### Likelihood Explanation
Any Bazel project using `java_import`/`java_library` with jars fetched from external, less-trusted sources (mirrors, third-party BCR-adjacent hosts) is exposed once `run_ijar`/`stamp_jar` is invoked over that jar, which is a default, common code path for Java compile-jar generation. No special build flags are required.

### Recommendation
Add explicit bounds checks (analogous to `EnsureRemaining`) in `InputZipFile::ProcessCentralDirEntry` before every field read and before the `memcpy` of the file name, validating `p`, `p + file_name_length`, `p + extra_field_length`, and `p + file_comment_length` all remain within `zipdata_in_ + input_file_->Length()`.

### Proof of Concept
Construct a minimal ZIP file whose central directory entry is placed at (or very near) the end of the file and whose `file_name_length` field (offset 28 within the CDH, i.e., right after the 16-byte skip plus the two 4-byte size fields) declares a value far larger than the number of bytes actually remaining in the mapped file (e.g., 0xFFFF). Running `ijar <crafted.jar> out.jar` (or exercising `java_common.run_ijar` on this jar through a `BuildIntegrationTestCase`/`src/test/shell/bazel` test) triggers a read past the end of the `mmap`'d region inside the unchecked `memcpy` in `ProcessCentralDirEntry`, observable as a crash under ASan/valgrind or as leaked adjacent bytes copied into the output.

### Citations

**File:** third_party/ijar/zip.cc (L302-330)
```text
bool InputZipFile::ProcessNext() {
  // Process the next entry in the central directory. Also make sure that the
  // content pointer is in sync.
  u8 compressed, uncompressed;
  u8 offset;
  if (!ProcessCentralDirEntry(central_dir_current_, &compressed, &uncompressed,
                              filename, PATH_MAX, &attr, &offset)) {
    return false;
  }

  // There might be an offset specified in the central directory that does
  // not match the file offset, so always update our pointer.
  p = zipdata_in_ + in_offset_ + offset;

  if (EnsureRemaining(4, "signature") < 0) {
    return false;
  }
  u4 signature = get_u4le(p);
  if (signature == LOCAL_FILE_HEADER_SIGNATURE) {
    if (ProcessLocalFileEntry(compressed, uncompressed) < 0) {
      return false;
    }
  } else {
    error("local file header signature for file %s not found\n", filename);
    return false;
  }

  return true;
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

**File:** third_party/ijar/zip.cc (L493-525)
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
```
