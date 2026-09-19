### Title
Heap buffer over-read in ijar's central-directory parser via unbounded field reads - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` reads a series of length-prefixed fields (file name, extra field, comment) from the mmap'd central directory of an attacker-supplied zip/jar and advances a raw pointer `p` by attacker-controlled 16-bit lengths without ever calling the `EnsureRemaining()` bounds check that the local-file-header parser (`ProcessLocalFileEntry`) does use.

### Finding Description
`ProcessLocalFileEntry` explicitly guards every variable-length read with `EnsureRemaining()` before advancing `p`: [1](#0-0) 

By contrast, `ProcessCentralDirEntry` — used both by `ProcessNext` (extraction path) and `CalculateOutputLength` — reads `compressed_size`, `uncompressed_size`, `file_name_length`, `extra_field_length`, `file_comment_length`, `attr`, and `offset` directly via `get_u4le`/`get_u2le`, then does `p += file_name_length; ... p += extra_field_length; ... p += file_comment_length;` with **no call to `EnsureRemaining`** at any point: [2](#0-1) 

Because the central directory location/size in `FindZipCentralDirectory` is only checked to be `<= in_length` in aggregate (`cd.central_dir_offset + cd.central_dir_size > in_length`), nothing prevents a single malicious central-directory-header record from declaring `file_name_length`/`extra_field_length`/`file_comment_length` values that make `p` walk past the end of the memory-mapped input file. This is directly analogous to the GetGhostNum class of bug in the reference CVE: a length/count field taken from untrusted file content is used to advance a read cursor without validating it against the buffer's real remaining size, producing an out-of-bounds (heap) read past the mapped region. The `memcpy` into the fixed `filename` buffer is bounded by `filename_size`, but the read source pointer `p` itself, and the subsequent `extra_p`/`get_u2le(extra_p)` walk of ZIP64 extra fields, are not validated against the mapped input's real end, so dereferencing `p` (via `get_u4le`, `get_u2le`) after `p` has run off the mapping is an out-of-bounds read of the process's heap-mapped memory.

### Impact Explanation
`ijar`/`zip.cc` is Bazel's own C++ zip/jar reader (compiled into the `ijar` and `zipper`/`unzipper` tools and linked into build actions that must inspect zip/jar contents, e.g. interface-jar generation and repository/archive processing). A crafted jar/zip whose bytes an untrusted origin can serve (e.g., an unpinned or hash-mismatched `http_archive`/`http_jar` URL, or any dependency artifact consumed as a zip/jar before/without hash verification) can trigger reads past the end of the mmap'd input buffer, corrupting process state or crashing the tool (SIGSEGV/heap over-read), matching the "denial of service via a crafted input" characterization of the analog CVE. Because the code never validates remaining bytes in `ProcessCentralDirEntry`, this is a genuine memory-safety defect in Bazel's own native zip-parsing code, not merely a third-party dependency issue.

### Likelihood Explanation
Any attacker who controls the bytes of a zip/jar file that is processed by `ijar`/`zipper` and whose declared central-directory entry lengths are not cross-checked against the file's actual size can trigger the bug deterministically — no special privileges beyond serving content are required. The likelihood of exploitation depends on whether the specific jar/zip in question is fetched without an enforced/verified integrity hash (many `http_archive`/`http_jar` usages omit `sha256`), in which case a hostile mirror/registry can supply the crafted bytes directly.

### Recommendation
Add `EnsureRemaining()` bounds checks in `ProcessCentralDirEntry` before every pointer advance derived from an untrusted length field (`file_name_length`, `extra_field_length`, `file_comment_length`, and the ZIP64 extra-field `data_size` walk), mirroring the checks already present in `ProcessLocalFileEntry`, and fail cleanly (returning `false`/calling `error()`) instead of allowing `p` to exceed `zipdata_in_ + input_file_->Length()`.

### Proof of Concept
A `BuildIntegrationTestCase`/native unit test analogous to `third_party/ijar/zip_headers_test.cc` can construct a minimal zip byte buffer containing:
1. A valid EOCD/central-directory-locator sequence so `FindZipCentralDirectory` succeeds and returns a `central_dir` pointer near the end of a small mmap'd buffer.
2. A single central directory header (`CENTRAL_FILE_HEADER_SIGNATURE`) whose `file_name_length`/`extra_field_length`/`file_comment_length` fields are set to large values (e.g., `0xFFFF`) that exceed the actual remaining bytes in the buffer.

Running this buffer through `InputZipFile::ProcessNext` (or `CalculateOutputLength`) causes `ProcessCentralDirEntry` to advance `p` past the mapped buffer and dereference (`get_u2le`/`get_u4le`) out-of-bounds memory, which under ASan manifests as a heap-buffer-overflow/over-read, analogous to the referenced GetGhostNum crash. (Note: I could not fully verify from the index whether an existing `BuildIntegrationTestCase`/shell test already exercises this exact malformed-central-directory path, or the exact call chain that invokes ijar on http_archive/http_jar-fetched jars in the current release; a Devin session with full repo access would be needed to confirm and add this regression test.)

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
