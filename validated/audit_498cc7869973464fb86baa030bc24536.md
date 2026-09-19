Based on my analysis, I've found a concrete analog in the bazel repository.

### Title
Unvalidated attacker-controlled central-directory offset causes integer-underflow bounds bypass and out-of-bounds pointer dereference - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessNext` in `third_party/ijar/zip.cc` computes a pointer into the mapped zip/jar file using the `local_header_offset` value taken verbatim from an attacker-supplied Central Directory Header, and the subsequent bounds check (`EnsureRemaining`) is defeated by unsigned integer underflow, exactly matching the CVE-2017-0350 bug class ("a value passed from a user... not correctly validated and used in an offset calculation").

### Finding Description
`InputZipFile::ProcessCentralDirEntry` reads the local-header `offset` field directly from attacker-controlled bytes via `get_u4le(p)` (and, if `U4_MAX`, an even wider attacker-controlled 64-bit value from the Zip64 extra field) with no range validation against the mapped file size: [1](#0-0) 

`ProcessNext` then uses this unvalidated offset directly in a pointer/offset calculation: [2](#0-1) 

The only guard is `EnsureRemaining`, which computes `in_offset = p - zipdata_in_` and `remaining = input_file_->Length() - in_offset`, both as `size_t`. If the attacker-supplied `offset` pushes `p` beyond `zipdata_in_ + Length()`, `in_offset > Length()` and the subtraction underflows to a huge value, so the check `n > remaining` never trips, allowing the subsequent `get_u4le(p)` and further parsing (`ProcessLocalFileEntry`) to dereference memory far outside the mapped file: [3](#0-2) 

This class parses jar/zip files that are not necessarily produced by Bazel itself — `ijar` (built from this same `zip.cc`) is run over Java library jars, including those obtained from external dependencies (e.g. via `http_jar`/`http_archive`) as part of building interface jars, so the "value from a user" is effectively bytes served by a hostile origin/mirror inside a jar whose declared `sha256`/`integrity` check happens at download time but whose *internal offsets* are never independently validated by the zip/jar parser that later processes it.

### Impact Explanation
An attacker able to serve a crafted `.jar`/`.zip` (as a dependency artifact, even if the outer bytes match a pinned digest) can encode a Central Directory Header with an out-of-range `local_header_offset` (or Zip64 64-bit offset) that pushes the read cursor arbitrarily far outside the mapped input buffer. Because the underflow makes `EnsureRemaining` report an enormous amount of "remaining" data, the code proceeds to dereference and read from that wild pointer, causing a crash (segfault / SIGSEGV, denial of the ijar action) or, depending on heap/mmap layout, an out-of-bounds read that could leak adjacent process memory into parsed fields (filenames, sizes) that are echoed into the output jar or error messages.

### Likelihood Explanation
Any consumer of `ijar`/`zip.cc`-based tooling that processes a zip/jar not fully re-validated for internal structural consistency (only its outer bytes are hash-pinned) is exposed. No credentials, no MITM, and no access to the victim's machine are required — only the ability to have the victim's build fetch and process an attacker-influenced jar. The bounds-check flaw is a simple unsigned-subtraction underflow with no compensating check elsewhere in the read path.

### Recommendation
In `EnsureRemaining`, validate that `p >= zipdata_in_` and `p <= zipdata_in_ + input_file_->Length()` (or equivalently check `in_offset <= Length()`) *before* computing `remaining = Length() - in_offset`, and reject/central-directory-abort if the local header offset (from either the 32-bit field or the Zip64 extra field) is not fully within `[0, Length())`. This should be enforced in `ProcessNext` immediately after computing `p`, before any further reads.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell` reproduction would construct a minimal zip with one Central Directory Header whose `local_header_offset` field (bytes 42-45 of the CDH) is set to `0xFFFFFFFE` (or, using a Zip64 extra field, an 8-byte offset such as `0x7FFFFFFFFFFFFFFF`), run it through `ijar`/`zipper` (`third_party/ijar/zip_main.cc`), and observe the process crash (SIGSEGV) rather than emitting a clean "corrupt archive" error — analogous to the existing `test_no_path_traversal` regression test in `third_party/ijar/test/zip_test.sh` [4](#0-3)  but exercising the offset field instead of the path field.

### Citations

**File:** third_party/ijar/zip.cc (L158-170)
```text
  // Check that at least n bytes remain in the input file, otherwise
  // abort with an error message.  "state" is the name of the field
  // we're about to read, for diagnostics.
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

**File:** third_party/ijar/zip.cc (L507-541)
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
