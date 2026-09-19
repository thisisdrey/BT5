### Title
Heap-buffer overflow via integer-underflow bypass of zip bounds check in ijar's `InputZipFile::EnsureRemaining` / `ProcessNext` - (File: `third_party/ijar/zip.cc`)

### Summary
The CVE-2026-24817 analog class here is Bazel's own ZIP/JAR extraction code path in `third_party/ijar/zip.cc` (functionally the "ZipDecompressor" surface for this repo), which is exercised whenever Bazel processes a JAR — including third-party JARs fetched via `http_jar`/`http_archive` and passed through the `ijar` tool to build interface JARs. The central-directory `offset` field is attacker-controlled and unsanitized before it is combined with the input buffer base pointer, and the subsequent bounds check performs an unsigned subtraction that can underflow, defeating the guard.

### Finding Description
`InputZipFile::ProcessCentralDirEntry` reads the per-entry `offset` field straight from the ZIP central directory with no range validation: [1](#0-0) 

`ProcessNext` then uses this unchecked `offset` to compute the read cursor `p`, jumping wherever the attacker specifies relative to the mapped input buffer: [2](#0-1) 

The only guard before dereferencing `p` is `EnsureRemaining`, which computes `remaining` via **unsigned** subtraction of the current in-buffer offset from the total mapped file length: [3](#0-2) 

If `offset` is large enough that `in_offset_` (`p - zipdata_in_`) exceeds `input_file_->Length()`, the subtraction `input_file_->Length() - in_offset` underflows to a huge `size_t`, so the check `n > remaining` never trips even though `p` now points far outside the mapped file. Execution proceeds to `get_u4le(p)` in `ProcessNext`/`ProcessLocalFileEntry`, dereferencing a wild pointer, and further length fields read from that wild location (`compressed_size_`, `uncompressed_size_`, `file_name_length_`, `extra_field_length_`) are then used to drive subsequent `memcpy`/`Process()` calls that copy attacker-influenced-length data into other buffers — an out-of-bounds read that cascades into an out-of-bounds write in downstream consumers of `Process()` (e.g. `UnzipProcessor::Process`, `combiners.cc`, or the interface-jar writer, all of which trust the `size`/`data` pointer pair supplied by the ZIP parser).

The same integer-underflow escape hatch also applies to `ProcessCentralDirEntry`'s ZIP64 extra-field walk, where `extra_p += data_size` (attacker-controlled `u2`) can overshoot the `p` sentinel used in `while (extra_p != p)`, letting the loop keep advancing arbitrarily far past the intended extra-field region before it happens to (or never does) land back on `p`: [4](#0-3) 

### Impact Explanation
A malicious JAR served by an unprivileged origin (e.g., a Maven artifact fetched with `http_jar`/`http_archive`, whose declared `sha256` matches the attacker's own malicious bytes — checksum verification passes because it protects transport integrity, not internal ZIP structural correctness) can carry a crafted central directory `offset`/extra-field `data_size` that steers Bazel's ZIP parser to read/write outside the mapped input or output buffer during `ijar` interface-jar generation. This is a heap out-of-bounds read/write inside a build tool that processes externally-supplied binary content, matching the CWE-787/CWE-125 class of the reference advisory (unbounded pointer walk over attacker-controlled length fields).

### Likelihood Explanation
Reaching this requires only that a JAR dependency consumed by a Bazel build (via any rule that runs it through `ijar`, e.g. Java interface-jar generation) contains a hand-crafted central directory entry — this needs no privileged access, no MITM, and no tampering after fetch since the checksum verifies exactly the attacker's bytes. The bug is in first-party Bazel code (`third_party/ijar/zip.cc`), not a third-party dependency, and triggers on default flags with no special configuration.

### Recommendation
- In `EnsureRemaining`, detect and reject `in_offset > input_file_->Length()` (i.e. detect underflow) instead of relying on unsigned subtraction: `if (in_offset > input_file_->Length()) return error(...);` before computing `remaining`.
- In `ProcessNext`, bound-check the raw `offset` value against `input_file_->Length()` before adding it to `zipdata_in_ + in_offset_`.
- In `ProcessCentralDirEntry`'s ZIP64 extra-field loop, bound each `extra_p` advance against the `p` sentinel (`if (extra_p + data_size > p) return error(...)`) instead of relying on exact pointer equality to terminate.

### Proof of Concept
A `BuildIntegrationTestCase`/unit test analogous to `third_party/ijar/zip_test.cc` should:
1. Construct a minimal valid ZIP/JAR with one central directory entry.
2. Patch the entry's 4-byte `offset` field (and separately, an extra field's `data_size`) to a value that makes `zipdata_in_ + in_offset_ + offset` land outside the mmap'd region, or that overshoots the extra-field end sentinel.
3. Feed this file to `ZipExtractor::Create(...)->ProcessAll()` and observe (under ASan) a heap-buffer-overflow read/write, or a SIGSEGV, instead of a clean `GetError()` diagnostic. [5](#0-4)

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

**File:** third_party/ijar/zip.cc (L513-515)
```text
  p += 4;  // skip to external file attributes field
  *attr = get_u4le(p);
  *offset = get_u4le(p);
```

**File:** third_party/ijar/zip.cc (L524-542)
```text
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
```
