### Title
Unbounded central-directory entry parsing in ijar's ZIP reader can read past the mapped file buffer - ([File: third_party/ijar/zip.cc])

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` reads `file_name_length`, `extra_field_length`, and `file_comment_length` (and, within the extra-field loop, per-record `data_size`) directly from attacker-controlled ZIP bytes and advances the parsing pointer `p`/`extra_p` by those values with **no check that the resulting pointer stays inside the mapped input file**. This is the same bug class as the referenced sp-trie issue: untrusted, length-prefixed record data is trusted to index/advance a cursor without validating it against the actual buffer bounds, unlike the sibling function `ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before every pointer advance.

### Finding Description
`ProcessLocalFileEntry` (third_party/ijar/zip.cc:332-419) is careful: before reading the filename or extra-field bytes it calls `EnsureRemaining(file_name_length_, ...)` / `EnsureRemaining(extra_field_length_, ...)` [1](#0-0) , aborting with an error if the declared length would run past the end of the mapped input.

`ProcessCentralDirEntry`, however, has no equivalent guard: [2](#0-1) 
- `file_name_length`, `extra_field_length`, `file_comment_length` are read as raw `u2` values from the file and used directly to advance `p` and to `memcpy` from `p` into a fixed `filename` buffer.
- The `while (extra_p != p)` loop then reads `header_id`/`data_size` from `extra_p` and advances `extra_p += data_size` [3](#0-2)  with no check that `data_size` doesn't overrun the extra-field region, or that `extra_p`/`p` stay within the mapped file at all.

The code comment claims safety ("the central directory is always followed by another data structure that has a signature, so parsing it this way is safe") [4](#0-3) , but that assumption only holds for a *well-formed* archive; it does not hold for a maliciously crafted one where these length fields are set larger than the remaining central directory / file size. `ijar` is the tool bundled with Bazel that strips a JAR down to its public interface (used implicitly for Java compilation, and reachable whenever Bazel processes a JAR obtained from an `http_archive`/`http_jar`/`java_import`-style fetch from a hostile or compromised origin). Because ijar reads the file via a memory-mapped buffer (`InputJar`/mapped file), advancing `p`/`extra_p` past the mapped region and then dereferencing it (`get_u4le`, `get_u2le`, `memcpy`) constitutes an out-of-bounds read of process memory — a crash (SIGSEGV) at best, a memory-disclosure primitive at worst, driven entirely by attacker-supplied archive bytes.

### Impact Explanation
An attacker who controls the bytes of a `.jar`/`.zip` consumed by Bazel's `ijar` tool (e.g., served at a dependency URL, from a hostile mirror, or committed to an untrusted branch that CI builds) can craft a central directory entry with an oversized `extra_field_length` or `data_size` to make ijar read/memcpy beyond the mapped file's end. This can crash the Bazel action worker (denial of service for that build) or leak adjacent heap/mapped memory bytes into the derived output artifact (the produced interface jar), since parsed extra-field bytes and filenames feed into subsequent processing/output.

### Likelihood Explanation
Reachable without any special privilege: any dependency archive fetched by `http_archive`/`http_jar`, any BCR-hosted or self-hosted registry archive, or any JAR checked into an untrusted branch that Bazel's Java rules process through ijar will trigger this code path. No mocked path is required — `ProcessCentralDirEntry` is on the direct decode path of `InputZipFile::CalculateOutputLength` and other ijar entry points that read every central directory record of an input archive. The missing bound check is a straightforward oversight relative to the adjacent, correctly-guarded `ProcessLocalFileEntry`, making the bug easy to trigger with a single malformed record.

### Recommendation
Add explicit bounds checks (mirroring `EnsureRemaining` used in `ProcessLocalFileEntry`) in `ProcessCentralDirEntry` before every pointer advance derived from untrusted length fields: verify `file_name_length`, `extra_field_length`, `file_comment_length`, and each extra-field `data_size` do not exceed the remaining bytes in the mapped file/central directory before advancing `p`/`extra_p`, and fail with a diagnostic (as `EnsureRemaining` does) rather than silently continuing to read out of bounds.

### Proof of Concept
A concrete reproduction would require constructing a ZIP/JAR whose central directory record advertises `extra_field_length` (or an inner extra-field `data_size`) larger than the number of bytes actually remaining before the end-of-central-directory record, then invoking ijar (`third_party/ijar/zip.cc`'s `InputZipFile::ProcessCentralDirEntry`, e.g., via `CalculateOutputLength` or the interface-jar-building entry point) on that file. I was not able to fully verify, within the available read-only search context, the exact boundary-setup code (`central_dir_` initialization and total mapped length) that would confirm the absence of an outer bounds check across the whole central-directory scan loop — this would need to be validated with a JUnit/shell test built against `ijar`'s public entry points (analogous to `src/test/shell/bazel/starlark_repository_test.sh`'s zip/ar extraction tests) to produce a reliably reproducible crash and confirm no upstream guard intercepts it.

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

**File:** third_party/ijar/zip.cc (L491-492)
```text
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
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

**File:** third_party/ijar/zip.cc (L526-541)
```text
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
