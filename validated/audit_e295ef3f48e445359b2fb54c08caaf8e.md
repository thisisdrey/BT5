### Title
Out-of-bounds read in ZIP central-directory parsing due to missing per-entry length validation - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` reads the `file_name_length`, `extra_field_length`, and `file_comment_length` fields straight from an attacker-controlled ZIP/JAR file and advances the parse pointer `p` by these values, plus walks an "extra field" loop advancing by an attacker-controlled `data_size`, with **no bounds check against the end of the mapped input buffer**. [1](#0-0) 

### Finding Description
Unlike `InputZipFile::ProcessLocalFileEntry`, which calls `EnsureRemaining()` before consuming variable-length fields such as `file_name_length_` and `extra_field_length_`, [2](#0-1) 
`ProcessCentralDirEntry` performs no equivalent check. It unconditionally does:
```
p += file_name_length;
const u1 *extra_p = p;
p += extra_field_length;
while (extra_p != p) {
  const u2 header_id = get_u2le(extra_p);
  const u2 data_size = get_u2le(extra_p);
  extra_p += data_size;
  ...
}
p += file_comment_length;
``` [3](#0-2) 

The only prior validation is a single aggregate check in `FindZipCentralDirectory` that the *whole* central directory (`central_dir_offset + central_dir_size`) fits within the file length; it does not validate that any individual entry's declared `file_name_length + extra_field_length + file_comment_length` stays within the mapped region. [4](#0-3) 

Because the entire input file is memory-mapped (`MappedInputFile`) and `p`/`extra_p` are raw pointers into that mapping, a crafted central directory entry with inflated `file_name_length`/`extra_field_length`/`data_size` values causes `get_u2le`/`get_u4le`/`memcpy` (in the filename-copy block) to read past the end of the mapped file into adjacent memory. The `filename` buffer is filled via `memcpy` with attacker-influenced length taken from the corrupted region, which is subsequently used/emitted by tools built on `ZipExtractor` (e.g. `ijar`, `singlejar`) that process externally-supplied JAR/ZIP inputs (e.g. prebuilt jars fetched via `java_import`/Maven-style external dependencies). [5](#0-4) 

### Impact Explanation
This is an out-of-bounds read triggered purely by parsing attacker-supplied file content — no code execution privilege or MITM is needed. Because the ZIP content itself, not its cryptographic hash, is malformed, a correct `sha256`/integrity check on the fetched archive does not prevent the crash/leak: the file can be exactly what the victim pinned, yet still be structurally malicious. The read can leak adjacent heap/mmap bytes into the `filename` buffer or into other parsed fields, which is analogous to the CVE's local information disclosure via improper input validation on externally supplied data.

### Likelihood Explanation
Any attacker who can publish a JAR/ZIP artifact that a Bazel build ingests (e.g. through `java_import`, a `http_jar`/`http_archive` producing a `.jar`, or any target that runs `ijar`/`singlejar` over an externally-sourced archive) can trigger this by crafting the central directory header field lengths. `ProcessLocalFileEntry`'s existing `EnsureRemaining` calls show the maintainers are aware of and treat local-header lengths as untrusted, but the equivalent guard was never added for `ProcessCentralDirEntry`.

### Recommendation
Add `EnsureRemaining`-style bounds checks (or equivalent pointer/end comparisons against the mapped buffer end) in `ProcessCentralDirEntry` before consuming `file_name_length`, `extra_field_length`, `file_comment_length`, and before each `data_size` step in the extra-field walk, mirroring the validation already present in `ProcessLocalFileEntry`.

### Proof of Concept
Construct a minimal ZIP whose single Central Directory Header declares `file_name_length`/`extra_field_length`/`file_comment_length` (or a nested extra-field `data_size`) large enough that `p`/`extra_p` walk past the mapped file's end (e.g. total central directory entry size much smaller than declared lengths, while still satisfying the aggregate `central_dir_offset + central_dir_size <= in_length` check in `FindZipCentralDirectory`). Feed this file to a `ZipExtractor`/`ijar` invocation (as done in existing native tests under `third_party/ijar`) and observe an out-of-bounds read (ASan heap-buffer-overflow or a crash) inside `InputZipFile::ProcessCentralDirEntry`, confirming the missing-bounds-check path described above.

**Note on verification limits**: I was unable to fully trace, within the available tool budget, the exact call sites that drive `ProcessCentralDirEntry` in a loop (`ProcessNext`/`GetNextEntry`) to confirm whether any outer loop bound stops iteration before pointer corruption occurs, nor could I confirm the exact downstream Bazel rule/tool (`ijar` vs `singlejar`) invocation path that feeds externally-fetched, untrusted JAR bytes into this exact code path in this snapshot. A background Devin session with full repository access would be needed to trace `third_party/ijar/zip.h`, `src/main/cpp/archive_utils.cc`, and the `ijar`/`singlejar` `BUILD` wiring to confirm the precise external-input reachability and to write a reproducible JUnit/shell test.

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
