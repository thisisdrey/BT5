## Finding: Out-of-bounds read when processing a malicious/truncated ZIP central directory in ijar

### Title
Out-of-bounds read in ijar's ZIP central directory parser due to unchecked header field lengths - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in ijar's ZIP reader parses each Central Directory Header (CDH) entry by reading 32/16-bit length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) directly from attacker-supplied archive bytes and then advancing the read cursor `p` by those attacker-controlled lengths — with **no bounds check against the mapped file's actual size**, unlike its sibling `ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before every field read.

### Finding Description
`InputZipFile::ProcessCentralDirEntry` is the CDH parser used when ijar processes a ZIP/JAR (e.g. when Bazel strips a jar to build an interface jar for `java_import`/`java_library` dependencies, including jars fetched from external, attacker-controllable sources such as `http_jar`, `maven_install`, or a hostile registry-hosted artifact): [1](#0-0) 

Unlike `ProcessLocalFileEntry`, which guards every read with `EnsureRemaining()`: [2](#0-1) 

`ProcessCentralDirEntry` performs no such check. It reads `file_name_length`, `extra_field_length`, and `file_comment_length` as raw 16-bit values straight from attacker content, then does:

```
memcpy(reinterpret_cast<void*>(filename), p, len);
...
p += file_name_length;
const u1 *extra_p = p;
p += extra_field_length;
while (extra_p != p) { ... get_u2le(extra_p) ... }
...
p += file_comment_length;
```

There is no verification that `p + file_name_length + extra_field_length + file_comment_length` stays within the memory-mapped input file. `FindZipCentralDirectory` only validates that `cd.central_dir_offset + cd.central_dir_size <= in_length` for the directory *as a whole* — it does not validate that each individual entry's declared lengths are consistent with the remaining bytes in that region. A crafted CDH entry with an oversized `file_name_length`/`extra_field_length`/`comment_length` moves `p` past the end of the mapped file, and the next call to `ProcessCentralDirEntry` (or the `while (extra_p != p)` loop parsing the fake "extra field") dereferences memory beyond the mapped input, reading whatever adjacent memory happens to follow the mmap. This is structurally identical to the CVE-2023-37459 pattern: a length/flag field from untrusted data is consumed to compute an offset, and a subsequent field access is performed before validating that the buffer actually contains that many bytes.

This mirrors the CVE's bug class precisely — reading a header field's declared size without validating remaining buffer length before dereferencing further fields, applied here to ZIP archive processing.

### Impact Explanation
An attacker who controls the content served at a jar/archive URL that Bazel's ijar tool processes (e.g., a hostile HTTP server or mirror serving a `http_jar`/`java_import` dependency, or a compromised release asset) can craft a ZIP central directory with out-of-range length fields. This causes ijar to read past the bounds of the memory-mapped input file, resulting in an out-of-bounds read (CWE-125) that can crash the build (SIGSEGV from an unmapped page) or, in the memcpy of `filename`, copy adjacent process memory into the observable `filename` buffer, which is later used in error/diagnostic output and in the generated interface jar's entry name — a potential information-disclosure vector.

### Likelihood Explanation
Ijar is invoked automatically by Bazel's Java toolchain (interface jar generation) on jars pulled in as dependencies, several of which can originate from untrusted URLs/registries an attacker publishes to (matching the "hostile origin server" attacker model). No sha256 mismatch is required to trigger this — the corruption is internal to a validly-checksummed byte stream, so per-file integrity checks (`sha256`/`integrity`) do not protect against this, since the attacker only needs the served bytes to match whatever hash was pinned by that same attacker-controlled artifact publication.

### Recommendation
In `ProcessCentralDirEntry` (and the extra-field parsing loop within it), validate that `p`, after each field length is read, does not exceed the mapped input's end (mirroring the `EnsureRemaining()` pattern already used in `ProcessLocalFileEntry`), and fail parsing with an error rather than continuing to advance/deref an out-of-bounds pointer.

### Proof of Concept
A JUnit/native test analogous to `zip_headers_test.cc`/`transient_bytes_test.cc` can construct a small in-memory ZIP with one CDH entry whose `file_name_length` (or `extra_field_length`/`comment_length`) is set to a large value exceeding the remaining central-directory bytes, feed it to `InputZipFile::Open`/`ProcessNext` via the existing `InputZipFile` test scaffolding, and show that under ASan the process reports a heap/mmap out-of-bounds read (or crashes) instead of ijar returning a graceful "corrupt archive" error, using the existing `EnsureRemaining`-based error path in `ProcessLocalFileEntry` as the reference for the expected safe behavior.

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
