## Title
Integer-underflow bypass of ZIP bounds check leads to out-of-bounds heap read when processing an untrusted jar/zip - (File: `third_party/ijar/zip.cc`)

## Summary
`InputZipFile::EnsureRemaining` guards every read of local-file-header fields with a size check computed by subtracting the current cursor offset from the mapped file length. The cursor (`p`) is set directly from an attacker-controlled 32/64-bit `offset` field taken from the ZIP central directory, with no validation that the offset is within the file's bounds before the subtraction is performed. This mirrors the `bulkMint`/`_internalMint` class of bug: a limit check expressed as `a - b < limit`/`n > remaining` is trusted to fail safely, but the attacker-controlled operand can drive the subtraction negative (here, unsigned wraparound) and silently defeat the check.

## Finding Description
`ProcessNext` reads a central-directory entry via `ProcessCentralDirEntry`, which returns an unvalidated `offset` field taken straight from attacker-controlled bytes (and possibly overridden by a Zip64 extra field, `get_u8le(extra)`, both fully attacker-controlled): [1](#0-0) 

That offset is used, unchecked, to reposition the read cursor arbitrarily within (or beyond) the mapped input file: [2](#0-1) 

The only guard before dereferencing `p` is `EnsureRemaining`, which computes:
```
size_t in_offset = p - zipdata_in_;
size_t remaining = input_file_->Length() - in_offset;
if (n > remaining) { ... error ... }
``` [3](#0-2) 

`in_offset` and `remaining` are `size_t` (unsigned). If the attacker-chosen `offset` pushes `p` past `zipdata_in_ + input_file_->Length()`, then `in_offset > Length()`, and `Length() - in_offset` wraps around to a huge unsigned value. The subsequent check `n > remaining` is then always false, so the bounds check is bypassed entirely — exactly the `balanceOf(...) < _maxAllowedPerWallet - numberOfMints` underflow pattern from the reference report, where the subtraction-based check silently passes when it should fail. Once bypassed, `get_u2le`/`get_u4le`/`memcpy` in `ProcessLocalFileEntry` and `ProcessCentralDirEntry` read out-of-bounds heap/mmap memory adjacent to the mapped input file: [4](#0-3) 
The same unchecked-subtraction pattern recurs in `UncompressFile`: [5](#0-4) 

This code underlies the `ijar`/`zipper` tools which are exercised whenever Bazel builds an interface jar for `java_import`/`java_library` from a jar that can originate from an externally-fetched artifact (e.g. a `.jar` retrieved via `http_jar`/`http_archive`). A sha256/integrity pin on the *whole file* does not protect against this: the attacker can craft any byte sequence they like (since they control the origin) and simply publish the checksum that matches their crafted file — the checksum only proves the file is the one the attacker intended, not that its internal ZIP structure is well-formed. The malicious central-directory offset is entirely internal file content, invisible to a whole-file hash check.

## Impact Explanation
Bypassing the bounds check lets the parser read attacker-influenced amounts of memory beyond the mapped input file (heap-adjacent memory in the `ijar`/`zipper` process). Data read this way (file names via `memcpy`, size/version fields) can be echoed into the generated interface jar or trigger further out-of-bounds reads/writes downstream (e.g., `compressed_size_`/`uncompressed_size_` derived from OOB memory driving subsequent large reads), producing a memory-disclosure and likely crash/DoS primitive when processing a hostile jar/zip. This does not require any credential or local access — only that the victim's build consumes a jar the attacker controls.

## Likelihood Explanation
Any Bazel build that fetches a `.jar`/`.zip` from an external source and runs it through `ijar` (interface jar generation for `java_import`/`java_library`, or direct use of the `zipper`/`ijar` binaries) is reachable. No integrity check currently protects against malformed *internal* ZIP structure — sha256/integrity only pins the outer bytes, and the attacker is free to choose those bytes. The offset field is a 32-bit (or Zip64 64-bit) value fully under attacker control with zero validation against the mapped file's length before use.

## Recommendation
In `EnsureRemaining` (and anywhere else the pattern `Length() - offset` appears, e.g. `UncompressFile`), validate that the cursor/offset is within `[zipdata_in_, zipdata_in_ + Length()]` before performing the subtraction, e.g. reject if `in_offset > input_file_->Length()` prior to computing `remaining`, rather than trusting the unsigned subtraction not to wrap. Apply the same explicit bounds check to the central-directory `offset` field (and its Zip64 override) in `ProcessCentralDirEntry`/`ProcessNext` before it is added to `zipdata_in_`.

## Proof of Concept
Not fully verified end-to-end due to index/tool limits — a full PoC would require crafting a ZIP/JAR whose central directory entry advertises a local-file-header `offset` (or Zip64 offset override) larger than the file's actual length, then invoking `ijar`/`zipper` (or a `java_import`/`http_jar` build depending on it) on that file and observing an out-of-bounds read (e.g., under ASan) instead of the expected "Premature end of file" error. This should be constructed as a `src/test/shell` integration test analogous to `third_party/ijar/test/zip_test.sh`'s existing `test_no_path_traversal`/large-file tests, feeding a hand-crafted malformed central-directory offset and asserting the tool fails safely rather than reading out of bounds: [6](#0-5)

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

**File:** third_party/ijar/zip.cc (L312-318)
```text
  // There might be an offset specified in the central directory that does
  // not match the file offset, so always update our pointer.
  p = zipdata_in_ + in_offset_ + offset;

  if (EnsureRemaining(4, "signature") < 0) {
    return false;
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

**File:** third_party/ijar/zip.cc (L437-441)
```text
u1* InputZipFile::UncompressFile() {
  size_t in_offset = p - zipdata_in_;
  size_t remaining = input_file_->Length() - in_offset;
  DecompressedFile *decompressed_file =
      decompressor_->UncompressFile(p, remaining);
```

**File:** third_party/ijar/zip.cc (L513-541)
```text
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
```

**File:** third_party/ijar/test/zip_test.sh (L277-283)
```shellscript
function test_no_path_traversal() {
  local folder=$(mktemp -d ${TEST_TMPDIR}/output.XXXXXXXX)
  ! (cd $folder && $ZIPPER x $(dirname ${ZIPPER})/test/path_traversal_zip.jar)
  if [[ -e ${folder}/../ZIPPER_POC_OWNED ]]; then
    fail "Path traversal succeeded"
  fi
}
```
