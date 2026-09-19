### Title
Out-of-bounds read of attacker-controlled central-directory fields in `InputZipFile::ProcessCentralDirEntry` - (File: third_party/ijar/zip.cc)

### Summary
`ijar`'s zip reader parses the central directory of any jar/zip file it processes (interface-jar generation runs automatically over jar inputs, including jars fetched from external repositories via `http_archive`/`http_jar`/`http_file` or supplied as `.jar`/`.srcjar` dependencies). `InputZipFile::ProcessCentralDirEntry` reads length fields directly out of the mapped input file and uses them to copy attacker-controlled bytes, without first validating that the declared lengths stay within the bounds of the mapped file — unlike the sibling local-file-header path, which explicitly calls `EnsureRemaining()` before every read.

### Finding Description
`InputZipFile::ProcessCentralDirEntry` (third_party/ijar/zip.cc) reads `file_name_length`, `extra_field_length`, and `file_comment_length` straight from the mapped central-directory bytes and then does: [1](#0-0) 
```
p += 16;  // skip to 'compressed size' field
*compressed_size = get_u4le(p);
*uncompressed_size = get_u4le(p);
u2 file_name_length = get_u2le(p);
u2 extra_field_length = get_u2le(p);
u2 file_comment_length = get_u2le(p);
...
memcpy(reinterpret_cast<void*>(filename), p, len);
```
There is no call to `EnsureRemaining()` (the bounds-check helper used elsewhere in the file, e.g. in `ProcessNext`/`ProcessLocalFileEntry`) before these reads or before the `memcpy`/pointer-advance operations (`p += file_name_length`, `p += extra_field_length`, `p += file_comment_length`, and the ZIP64 extra-field loop that reads `header_id`/`data_size` and does `get_u8le(extra)` on caller-declared offsets). If a hostile-origin zip/jar declares a `file_name_length`, `extra_field_length`, or ZIP64 extra-field `data_size` that runs past the actual mapped file content (e.g., a truncated or crafted central directory entry near the end of the mapped region), the code reads and copies memory beyond the buffer that was allocated/mapped for the file's contents — the bug class matches CVE-2022-3715's "heap-buffer overflow in parameter_transform" (an unchecked length-driven buffer transform on attacker-controlled input).

This function is invoked both by `ProcessNext()` (used by every ijar/interface-jar generation pass over a jar input) and by `CalculateOutputLength()`, both of which loop over central-directory entries whose count and total size are themselves taken from the (also attacker-controlled) end-of-central-directory record found by `FindZipCentralDirectory`. `FindZipCentralDirectory` bounds-checks `central_dir_offset + central_dir_size <= in_length`, but this only limits the aggregate central directory region — it does not validate that each individual entry's declared `file_name_length`/`extra_field_length`/`file_comment_length` stays inside that already-validated region, so a single corrupted entry can still push `p` past the mapped buffer end within the overall loop.

### Impact Explanation
An attacker able to serve or place a crafted `.jar`/`.zip` (e.g., an `http_jar`/`http_archive` dependency from a hostile mirror, or a malicious file on an untrusted branch that CI builds and that ends up processed by `ijar`) can trigger an out-of-bounds memory read while Bazel's `ijar` tool parses the central directory. Depending on heap/mapping layout this can crash the process (denial of service) or, in the worst case, leak adjacent memory content into the copied `filename` buffer, matching a "heap-buffer overflow" class memory-safety violation, consistent with the CVE-2022-3715 analog requested. Because `ijar` runs natively (no JVM bounds checking), unlike Bazel's Java-side archive extraction, this is a genuine native memory-safety issue rather than a Java exception.

### Likelihood Explanation
Reaching this code only requires supplying a corrupt/malicious ZIP-format file as a build dependency processed through `ijar`, which is a normal and automatic part of the Java build graph (interface jars are generated transparently). No privileged access, only the ability to have a victim's build fetch/consume attacker-controlled archive bytes is needed, matching the required unprivileged-attacker threat model. Whether a corrupt length field actually reaches unmapped memory (crash) vs. merely reads adjacent bytes within the same mapped page depends on file size/page alignment and the underlying `MappedInputFile` implementation (mmap-based on POSIX, per `third_party/ijar/mapped_file_unix.cc`); I was not able to fully verify within the remaining budget whether the POSIX mapping is padded to page size (which would only crash on reads far past the last page) or whether the effective allocation size makes a heap-overflow-style read of adjacent process memory reliably reachable. This uncertainty should be resolved with the PoC before treating this as a fully confirmed, exploitable-for-disclosure bug rather than a crash-only issue.

### Recommendation
Add the same `EnsureRemaining()` bounds validation used in `ProcessLocalFileEntry`/`ProcessNext` to `ProcessCentralDirEntry` before reading `file_name_length`, `extra_field_length`, `file_comment_length`, and before the `memcpy` and before each `p +=` advance, as well as inside the ZIP64 extra-field loop before `get_u8le(extra)` reads. Validate that `p + file_name_length`, `p + extra_field_length`, `p + file_comment_length`, and each ZIP64 extra sub-field's `data_size` remain within `zipdata_in_ + input_file_->Length()` before dereferencing, failing the parse with a diagnostic (as is already done for corrupt/missing signatures) instead of reading unchecked.

### Proof of Concept
A `src/test/shell` or native gtest-style proof would construct a minimal zip file whose end-of-central-directory record is valid (so `FindZipCentralDirectory` succeeds) but whose single central-directory entry declares `file_name_length` (or `extra_field_length`) large enough that `p + file_name_length` exceeds `zipdata_in_ + input_file_->Length()`, then invoke `ZipExtractor::Create`/`ProcessAll` on it (mirroring `third_party/ijar/test/ijar_test.sh`'s `test_corrupt_eocd_zip`, which already demonstrates the harness aborts on some corrupt inputs) and run it under AddressSanitizer to confirm an out-of-bounds read is reported by `memcpy`/`get_u2le` in `ProcessCentralDirEntry`. I was not able to execute this PoC in this environment; it should be validated by a Devin session with build/ASan tooling before treating it as fully confirmed.

### Citations

**File:** third_party/ijar/zip.cc (L507-523)
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
```
