Investigating [1](#0-0) , I found the strongest reachable analog for this bug class: the central-directory-driven local-header lookup in `InputZipFile::ProcessNext` bypasses its own bounds check due to unsigned‑integer underflow, letting an attacker-controlled zip/jar direct the parser to read outside the mapped archive buffer — structurally the same failure mode as the FreeRTOS DNS parser (a length/offset field taken at face value defeats the bounds check that is supposed to constrain the read).

### Title
Buffer Over-Read via Unchecked Central-Directory Offset in ijar Zip Parser - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessNext` computes the local file header address directly from the attacker-controlled `local_header_offset` field of a Central Directory Header (CDH) entry, without validating that the resulting pointer lies inside the memory-mapped archive. The subsequent bounds check, `EnsureRemaining`, is defeated by unsigned-integer underflow when the computed offset places the cursor before or far beyond the mapped region, allowing the parser to read (and downstream code to copy/decompress) memory outside the archive's mapped bounds.

### Finding Description
`ProcessCentralDirEntry` reads `*offset = get_u4le(p);` straight from attacker-supplied bytes with no range validation against the file size [2](#0-1) . That raw value is then used to reposition the parsing cursor:

```
p = zipdata_in_ + in_offset_ + offset;
if (EnsureRemaining(4, "signature") < 0) { return false; }
u4 signature = get_u4le(p);
``` [3](#0-2) 

`EnsureRemaining` is supposed to be the containment check:
```
int EnsureRemaining(size_t n, const char *state) {
  size_t in_offset = p - zipdata_in_;
  size_t remaining = input_file_->Length() - in_offset;
  if (n > remaining) { return error(...); }
  return 0;
}
``` [4](#0-3) 

Because `in_offset` and `remaining` are computed as `size_t` (unsigned) from pointer arithmetic that is not first validated, a crafted `offset` (e.g., a value close to `UINT32_MAX`, or one that places `p` before `zipdata_in_`) makes `in_offset` wrap to a huge unsigned value. `remaining = Length() - in_offset` then also underflows to a value close to `SIZE_MAX`, so `n > remaining` is always false and the check silently passes even though `p` points far outside the mapped archive. The code then dereferences `p` via `get_u4le(p)` and proceeds into `ProcessLocalFileEntry`, which further reads `file_name_length_`/`extra_field_length_` and copies/decompresses data from this out-of-bounds region [5](#0-4) .

This is directly analogous to CVE-2024-38373: a declared length/offset field from untrusted, parsed content is trusted to compute a read boundary, and the mismatch between the declared value and the real buffer extent is not caught, producing a read past the intended buffer.

### Impact Explanation
An attacker who supplies a malicious `.jar`/`.zip` (e.g., a Maven/http_archive dependency or any archive ijar processes, such as when constructing interface jars from downloaded Java dependencies) can craft a CDH `local_header_offset` that forces the parser to read memory outside the mmap'd archive. This is an out-of-bounds read that can leak adjacent process memory into the interface jar output or crash the build (SIGSEGV) depending on address-space layout — a concrete "read outside the repository/exec-root boundary" of the archive being parsed.

### Likelihood Explanation
Reaching this requires only that the victim's build parses an attacker-provided zip/jar file, which is a common flow for external dependencies. No credentials, MITM, or local machine access is needed — the malicious bytes originate entirely from the hostile artifact content. The bounds-check underflow is a straightforward arithmetic property of `size_t` subtraction and requires no timing or race condition.

### Recommendation
Validate `offset` (and derivatively `in_offset_`) against `input_file_->Length()` *before* forming the pointer `p = zipdata_in_ + in_offset_ + offset`, rejecting any offset that would place `p` before `zipdata_in_` or beyond the end of the mapped file. `EnsureRemaining` should perform the comparison using signed/checked arithmetic (or explicitly verify `in_offset <= Length()` before subtracting) rather than relying on unsigned wraparound to be benign.

### Proof of Concept
A JUnit/`src/test/shell/bazel` style reproduction:
1. Build a minimal valid ZIP with one CDH entry.
2. Patch the CDH's `local_header_offset` field to `0xFFFFFFF0` (a value close to `UINT32_MAX`), which is well beyond the tiny archive's actual size.
3. Run ijar (`third_party/ijar/zip_main.cc` `extract`/interface-jar generation path) against this crafted archive.
4. Observe that `EnsureRemaining(4, "signature")` in `InputZipFile::ProcessNext` does not reject the request (due to unsigned underflow) and `get_u4le(p)` dereferences memory outside the mapped region, causing a crash or reading unrelated memory content into the tool's output, instead of ijar cleanly erroring with "Premature end of file".

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

**File:** third_party/ijar/zip.cc (L332-371)
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
