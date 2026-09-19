### Title
Out-of-bounds heap read in ijar's ZIP parser via integer-underflow bypass of the bounds check - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::EnsureRemaining` in `third_party/ijar/zip.cc` computes the number of "remaining" bytes as an unsigned subtraction of the current cursor offset from the mapped input length. When an attacker-controlled central-directory `offset` field pushes the cursor `p` past the end of the mapped file, this subtraction underflows to a huge `size_t`, silently defeating the bounds check and allowing subsequent reads (`get_u4le`, `get_u2le`, `memcpy`) to dereference memory far outside the mmap'd ZIP buffer — the same bug class as CVE-2021-44269 (an oversized/tainted length value driving a pointer past the valid heap region).

### Finding Description
`InputZipFile::ProcessNext` reads a per-entry `offset` value straight out of the central directory via `ProcessCentralDirEntry` [1](#0-0) , and then repositions the input cursor with no upper-bound validation on `offset`: [2](#0-1) 

The only guard is `EnsureRemaining(4, "signature")`, implemented as: [3](#0-2) 

`in_offset = p - zipdata_in_` and `remaining = input_file_->Length() - in_offset` are both computed with unsigned (`size_t`) arithmetic. If the attacker-supplied `offset` (a `u8`, extendable to full 64-bit range via the ZIP64 extra field handling at lines 531-541) makes `p` point beyond `zipdata_in_ + Length()`, then `in_offset > Length()` and the subtraction underflows, producing a near-`SIZE_MAX` value for `remaining`. The check `n > remaining` (`4 > huge_number`) is then false, so `EnsureRemaining` returns success even though `p` is a wild, out-of-bounds pointer. The subsequent `get_u4le(p)` read at line 319 (and any further parsing of `file_name_length`, `extra_field_length`, `memcpy` of the filename, etc., in `ProcessLocalFileEntry`) then dereferences memory beyond the heap allocation backing the mmap'd ZIP file — an out-of-bounds heap read, directly analogous to the tainted-`cnt`/`sptr` overrun in the WavPack CVE.

The same underflow pattern recurs in `InputZipFile::UncompressFile`, which recomputes `remaining` without ever calling `EnsureRemaining` first: [4](#0-3) 

If the compressed/uncompressed entry offsets or sizes were manipulated to leave `p` past the buffer end, `remaining` underflows here too, and the resulting enormous `bytes_avail` is handed to `Decompressor::UncompressFile`, which sets `stream.avail_in = bytes_avail` for zlib's `inflate()` [5](#0-4) , letting zlib read arbitrarily far past the mapped input.

### Impact Explanation
This is a memory-safety violation (heap OOB read) in the ijar ZIP/JAR parser, which Bazel invokes as part of normal Java build/interface-jar generation on `.jar` inputs (including jars originating from `http_archive`/`http_jar`/Maven-style external dependencies). An attacker who controls a dependency's published archive content (matching whatever checksum is declared for that specific malicious version — i.e., a "legitimate" but hostile release) can craft a ZIP with an out-of-range central-directory `offset`/`extra_field` entry that triggers the bounds-check underflow, causing the ijar/ZIP-processing binary to read out-of-bounds heap memory. This can crash the build (denial of build) or, depending on heap layout, leak adjacent heap contents into observable output (e.g., through error messages or emitted file data), which is a confidentiality/integrity concern beyond simple crash. This is a memory-safety bug in Bazel's own C++ code, not a third-party dependency, and it is triggered purely by attacker-published archive bytes reaching Bazel's own untrusted-content-processing path.

### Likelihood Explanation
The `offset` value is an attacker-fully-controlled 64-bit field from the ZIP central directory (extendable via the ZIP64 extra field, `header_id == ZIP64_EXTRA_FIELD_TAG`) with zero range validation before being added to the base pointer. The vulnerable `EnsureRemaining` bounds check is the sole gate and is provably bypassable through simple unsigned-integer underflow with no special conditions (any `offset` value that pushes `p` past `zipdata_in_ + Length()` triggers it). This makes the flaw straightforward to trigger with a hand-crafted malformed ZIP/JAR file — no race conditions, no privileged access, and no reliance on network MITM are required.

### Recommendation
In `InputZipFile::EnsureRemaining`, validate that `p >= zipdata_in_` and `in_offset <= input_file_->Length()` before performing the subtraction (or perform the comparison as `p > zipdata_in_ + input_file_->Length() - n` using pointer comparisons that cannot underflow), and reject the entry with an error(...) return rather than continuing. Additionally, validate the central-directory `offset` field against `input_file_->Length()` immediately after `ProcessCentralDirEntry` returns, before recomputing `p`, and apply the same rewritten bounds check in `InputZipFile::UncompressFile`.

### Proof of Concept
A JUnit/`src/test/shell/bazel` proof would construct a minimal ZIP file containing:
1. One local file header (arbitrary small content).
2. A central directory entry for that file whose `offset` field (bytes at the appropriate location after the 16-byte skip in `ProcessCentralDirEntry`) is set to a very large value, e.g., `0xFFFFFFF0` (or, using a ZIP64 extra field, a 64-bit offset such as `0xFFFFFFFFFFFF0000`), while keeping compressed/uncompressed sizes small and valid.
3. A valid End Of Central Directory record so the file parses as a well-formed ZIP up through the central directory.

Running the `ijar` binary (or any Bazel-internal path that constructs an `InputZipFile` over this buffer, e.g. via `ZipExtractor::Create` + `ProcessNext`) on this file should be shown, under a memory-sanitized build (ASan), to trigger a heap-buffer-overflow read at the `get_u4le(p)` call in `InputZipFile::ProcessNext` (`third_party/ijar/zip.cc` line 319), confirming that `EnsureRemaining`'s underflowed `remaining` value failed to reject the malicious offset.

### Citations

**File:** third_party/ijar/zip.cc (L161-170)
```text
  int EnsureRemaining(size_t n, const char *state) {
    size_t in_offset = p - zipdata_in_;
    size_t remaining = input_file_->Length() - in_offset;
    if (n > remaining) {
      return error("Premature end of file (at offset %zd, state=%s); "
                   "expected %zd more bytes but found %zd.\n",
                   in_offset, state, n, remaining);
    }
    return 0;
  }
```

**File:** third_party/ijar/zip.cc (L302-319)
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
```

**File:** third_party/ijar/zip.cc (L437-455)
```text
u1* InputZipFile::UncompressFile() {
  size_t in_offset = p - zipdata_in_;
  size_t remaining = input_file_->Length() - in_offset;
  DecompressedFile *decompressed_file =
      decompressor_->UncompressFile(p, remaining);
  if (decompressed_file == NULL) {
    if (decompressor_->GetError() != NULL) {
      error(decompressor_->GetError());
    }
    return NULL;
  } else {
    compressed_size_ = decompressed_file->compressed_size;
    uncompressed_size_ = decompressed_file->uncompressed_size;
    u1 *uncompressed_data = decompressed_file->uncompressed_data;
    free(decompressed_file);
    p += compressed_size_;
    return uncompressed_data;
  }
}
```

**File:** third_party/ijar/zip.cc (L493-515)
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
```

**File:** third_party/ijar/zlib_client.cc (L73-87)
```text
DecompressedFile *Decompressor::UncompressFile(const u1 *buffer,
                                               size_t bytes_avail) {
  z_stream stream;

  stream.zalloc = Z_NULL;
  stream.zfree = Z_NULL;
  stream.opaque = Z_NULL;
  stream.avail_in = bytes_avail;
  stream.next_in = const_cast<Bytef*>(reinterpret_cast<const Bytef*>(buffer));

  int ret = inflateInit2(&stream, -MAX_WBITS);
  if (ret != Z_OK) {
    error("inflateInit: %d\n", ret);
    return NULL;
  }
```
