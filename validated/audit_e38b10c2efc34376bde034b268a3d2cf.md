### Title
Heap buffer over-read/overflow in ZIP central-directory parsing when processing an attacker-supplied jar/zip - ([File: third_party/ijar/zip.cc])

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` parses the central directory of a ZIP/JAR file without any bounds checking against the mapped input buffer, unlike the sibling function `ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before every read. This mirrors the CVE-2021-26252 bug class: a parser trusts attacker-controlled length fields (`file_name_length`, `extra_field_length`, `file_comment_length`, and nested extra-field `data_size`) to advance a cursor and perform `memcpy`/field reads, with no validation that the cursor stays inside the allocated/mmapped buffer.

### Finding Description
`ProcessCentralDirEntry` reads the central directory header fields directly via `get_u4le`/`get_u2le` and then: [1](#0-0) 
advances `p` by `file_name_length`, then by `extra_field_length` while iterating internal extra-field records driven by an attacker-controlled `data_size`, and finally by `file_comment_length` — none of these advances are checked against the end of the memory-mapped input file. Contrast this with `ProcessLocalFileEntry`, a few dozen lines earlier in the same file, which validates every read with `EnsureRemaining()`: [2](#0-1) 
The `memcpy` into the caller-provided `filename` buffer in `ProcessCentralDirEntry` also copies directly from `p` for up to `file_name_length` bytes with no verification `p + len` is within the mapped file: [3](#0-2) 
The code comment asserts this is "safe" because the central directory is always followed by another signed data structure, but that invariant is not enforced — it is merely assumed of well-formed archives, and is exactly the kind of trust-in-attacker-supplied-length-fields bug that the analogous htmldoc CVE exploits (heap buffer overflow from unchecked length-driven pointer arithmetic in `pspdf_prepare_page`). `InputZipFile::ProcessNext` and the driving loop calls this function directly on `central_dir_current_`, which walks over attacker-controlled bytes of a downloaded jar/zip file: [4](#0-3) 

This code path (`third_party/ijar`) is Bazel's bundled `ijar`/`singlejar`-adjacent zip-reading library, used to process JAR files that are consumed as build inputs (e.g., via `java_import`, or when Bazel post-processes JARs fetched through `http_archive`/`http_jar` rules). An attacker who controls the content of a JAR/zip that a victim's build downloads (a hostile mirror, or a JAR embedded inside a compromised release archive whose sha256 pins only the outer archive, not necessarily protecting against crafted inner-archive metadata) can craft central directory length fields to push `p` past the end of the mapped buffer.

### Impact Explanation
An out-of-bounds read (and potential subsequent out-of-bounds copy via the `memcpy`) on a memory-mapped file can crash the process (`SIGSEGV`, reading past the mapping) or, depending on heap layout of `filename`/adjacent structures, corrupt or leak adjacent memory content into the `filename` buffer that is later used for `Accept`/`Process` callbacks. Because `ijar` runs on JAR files during action execution as an ordinary local tool (not sandboxed against its own input parsing bugs), this is a memory-safety violation triggered purely by content of an attacker-supplied jar/zip.

### Likelihood Explanation
Reaching this code requires only that the victim's build processes an attacker-influenced JAR/zip file through the ijar zip reader — a realistic scenario for `java_import`/interface-jar generation on dependencies fetched from external registries or mirrors. No credentials, sandbox escape, or MITM is required — the malicious bytes only need to be served as the archive's contents; existing sha256/integrity checks on the outer download do not validate the internal consistency of the central-directory bookkeeping fields being bounds-safe, since the checksum only confirms the file is byte-identical to what was pinned, not that ijar's own parser handles arbitrary well-checksummed byte layouts safely.

### Recommendation
Add explicit bounds checks (mirroring `EnsureRemaining()` used in `ProcessLocalFileEntry`) in `ProcessCentralDirEntry` before every field read and before every pointer advance (`file_name_length`, `extra_field_length`, iteration over extra-field records bounded by `data_size`, and `file_comment_length`), rejecting the archive with an error if the cursor would move past the mapped-file end at any point.

### Proof of Concept
A JUnit/`BuildIntegrationTestCase`-style PoC would: construct a minimal ZIP file whose Central Directory Header at the very end of the file declares a large `file_name_length`/`extra_field_length`/`file_comment_length` (or a crafted extra-field `data_size`) that, when added to the header's offset, exceeds the actual mapped file size; feed this file to `ZipExtractor::Create`/`ProcessNext` (as exercised by ijar's `main.cc` or singlejar); and observe an out-of-bounds read/crash (e.g., under ASan) instead of a graceful parse error, contrasting with `ProcessLocalFileEntry`'s `EnsureRemaining` guard which correctly rejects analogous malformed local headers.

*Note: I was unable to fully trace every call site that feeds attacker-controlled JAR bytes into `ijar`'s zip reader (e.g., confirming end-to-end reachability from `http_archive`/`http_jar` through to `ijar` invocation in the action graph) within the available search iterations; this should be verified with a live checkout before treating the PoC as final.*

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

**File:** third_party/ijar/zip.cc (L507-525)
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
```
