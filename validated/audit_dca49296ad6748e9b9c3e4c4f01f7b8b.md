### Title
Out-of-bounds read in `InputZipFile::ProcessCentralDirEntry` due to unbounded extra-field scan loop - (File: `third_party/ijar/zip.cc`)

### Summary
`third_party/ijar/zip.cc`'s central-directory parser scans a zip/jar entry's "extra fields" region using a `while (extra_p != p)` loop keyed on attacker-controlled 16-bit `data_size` fields, with no check that `extra_p` stays within the bounds of the extra-field region (or the mapped file at all) before it is dereferenced again via `get_u2le`/`get_u4le`/`get_u8le`.

### Finding Description
`InputZipFile::ProcessCentralDirEntry` reads a central directory header from the memory-mapped/attacker-supplied zip file and then walks the per-entry "extra fields": ` [1](#0-0) `

```
p += 16;  // skip to 'compressed size' field
...
u2 extra_field_length = get_u2le(p);
...
p += file_name_length;
const u1 *extra_p = p;
p += extra_field_length;
while (extra_p != p) {
  const u2 header_id = get_u2le(extra_p);
  const u2 data_size = get_u2le(extra_p);
  const u1 *extra = extra_p;
  extra_p += data_size;
  ...
}
```
` [2](#0-1) `

The loop terminates only when `extra_p` becomes exactly equal to `p` (the pointer computed as `file_name_end + extra_field_length`). Both `extra_field_length` and each per-record `data_size` are 16-bit values taken directly from attacker-controlled bytes in the jar/zip's central directory. If a crafted `data_size` causes `extra_p` to advance past `p` without ever landing exactly on it (e.g., an odd combination of sizes so `extra_p` "jumps over" the target address), the loop condition `extra_p != p` remains true indefinitely: `extra_p` keeps advancing (or, with a `data_size` of 0, can even fail to reach `p` while other fields are misread) past the intended extra-field region and continues calling `get_u2le`/`get_u8le`, which unconditionally dereference `extra_p[0..n]` with no bounds check against the mapped file end. This directly parallels the PHP `_php_iconv_mime_decode` bug: an unsigned/pointer scan variable is advanced by attacker-influenced amounts inside a loop whose exit condition can be skipped over, leading to reads beyond the intended buffer.

Unlike the local-file-header path (`InputZipFile::ProcessLocalFileEntry`), which calls `EnsureRemaining()` before consuming `file_name_length_`/`extra_field_length_` bytes (` [3](#0-2) `), the central-directory extra-field scan performs no equivalent `EnsureRemaining`/end-of-buffer check inside the `while (extra_p != p)` loop or on `extra_p` itself before each `get_u2le` call.

### Impact Explanation
`ijar`/`zip.cc` is used by Bazel to read and process zip/jar files, including archives fetched via `http_archive`/`http_jar` and processed as part of the build (e.g., interface jar generation, `zip_main.cc` unzip tool). Since the central directory of a jar is fully attacker-controlled content, a malicious/compromised artifact source (a hostile mirror, or a byte-for-byte unmodified but sha256-mismatched or unpinned archive) could ship a zip whose central directory extra-field lengths are crafted to make `extra_p` overshoot `p`, causing the parser to read arbitrary out-of-bounds memory past the mapped file (heap-buffer-overflow class read), potentially crashing the Bazel-invoked tool (denial of service) or, depending on subsequent processing/copy of the read bytes, leaking process memory content into build outputs.

### Likelihood Explanation
The looping and pointer arithmetic in `ProcessCentralDirEntry` are driven entirely by two attacker-controlled 16-bit fields (`extra_field_length`, and per-record `data_size`), and the exit condition is a strict pointer-equality check rather than a bounds/less-than check, so no data integrity/sha256 verification would prevent a maliciously crafted (but validly checksummed, since the attacker controls what bytes get shipped and hashed) archive from triggering this. This is directly analogous to the PHP `str_left`/`p1` unsigned wraparound bug, though here the failure mode is "pointer overshoots the target and never becomes equal" rather than integer wraparound to a huge value; both stem from using an exact-match/decrement-to-zero termination condition on an attacker-influenced stepping variable instead of a monotonic bounds check.

### Recommendation
Change the extra-field scan loop condition from `extra_p != p` to a bounds check such as `extra_p < p && extra_p + 4 <= p`, and validate that `extra_p + data_size <= p` before advancing, mirroring the bounds checks already used in `ExtraField::find` in `src/tools/singlejar/zip_headers.h` (` [4](#0-3) `), which explicitly checks `byte_ptr(start) + extra_field->size() > byte_ptr(end)` before dereferencing further.

### Proof of Concept
A concrete PoC would require constructing a crafted jar/zip file whose central directory entry has `extra_field_length` and an internal `data_size` chosen such that `extra_p` (starting at the extra-fields region) never becomes pointer-equal to `p = extra_p_start + extra_field_length` (e.g., by using an odd/overshooting `data_size` on the last record so `extra_p` skips past `p`), then invoking the `ijar`/`zip_main` central-directory reading path (`InputZipFile::ProcessCentralDirEntry`) on that file and observing an ASan heap-buffer-overflow read past the mapped file region — analogous to the reporter's PHP PoC. I was not able to fully trace/execute this within the available tools (no build/execution environment), so this PoC is described but not independently reproduced; a background Devin session with build tooling would be needed to construct the exact byte layout and confirm the crash under ASan/valgrind on a current release, since the exact wraparound/overshoot arithmetic needs to be validated against real header sizes.

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

**File:** third_party/ijar/zip.cc (L507-542)
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
  }
```

**File:** src/tools/singlejar/zip_headers.h (L99-114)
```text
  static const ExtraField* find(uint16_t tag, const uint8_t* start,
                                const uint8_t* end) {
    while (start < end) {
      if (ziph::byte_ptr(start) + sizeof(ExtraField) > ziph::byte_ptr(end)) {
        break;
      }
      auto extra_field = reinterpret_cast<const ExtraField*>(start);
      if (ziph::byte_ptr(start) + extra_field->size() > ziph::byte_ptr(end)) {
        break;
      }
      if (extra_field->is(tag)) {
        return extra_field;
      }
      start = ziph::byte_ptr(start) + extra_field->size();
    }
    return nullptr;
```
