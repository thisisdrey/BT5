Based on my investigation, I found a strong analog in the ijar zip extraction code, though I was unable to fully verify the `ProcessCentralDirEntry` implementation body (only found its call sites and declaration) within my remaining searches.

### Title
Stack-based buffer over-read/overflow risk in ZIP filename copy - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile` stores central-directory-derived filenames in a fixed-size stack buffer `char filename[PATH_MAX]` [1](#0-0) , and `ProcessNext()` calls `ProcessCentralDirEntry(central_dir_current_, ..., filename, PATH_MAX, ...)` passing that fixed-size buffer and its bound [2](#0-1) . This mirrors the unrar CVE-2017-14122 bug class: a stack-based buffer is populated from attacker-controlled archive entry name fields (`file_name_length_`, `extra_field_length_` are read directly from the file with `get_u2le`) [3](#0-2) .

### Finding Description
`ProcessLocalFileEntry` reads `file_name_length_` as an unchecked `u2` from the local file header and only validates that enough bytes *remain in the input file* via `EnsureRemaining`, not that the name fits any destination buffer [4](#0-3) . Separately, the central directory parsing path (`ProcessCentralDirEntry`) writes into the caller-supplied `char filename[PATH_MAX]` stack array using a filename length taken from attacker-supplied central-directory bytes. I could not retrieve the body of `ProcessCentralDirEntry` in this session to confirm whether it truncates/bounds-checks against the `filename_size` parameter (`PATH_MAX`) before copying — this is the key detail that determines whether a crafted ZIP with a central-directory `file_name_length` exceeding `PATH_MAX` triggers a stack buffer overflow/over-read, analogous to unrar's `ExtrFile`/`stricomp` over-read from unterminated/oversized name fields into a fixed buffer.

### Impact Explanation
If `ProcessCentralDirEntry` does not clamp/reject names longer than `PATH_MAX` before writing into `filename`, an attacker who supplies a malicious `.zip`/`.jar` (e.g., via `http_archive` fetched from a hostile mirror, or a JAR consumed by `singlejar`/`ijar`) could corrupt the stack, potentially leading to memory corruption during the build's own extraction/repackaging tooling — impacting confidentiality and availability of the build.

### Likelihood Explanation
Reachability requires only that a build consume an attacker-influenced ZIP/JAR file (e.g., a dependency archive fetched via `http_archive`), which is a common workspace/module extension pattern and does not require privileged access — matching the unprivileged attacker model. However, without confirming the actual bounds-check logic inside `ProcessCentralDirEntry`, I cannot assert this is exploitable in the current release; it may already be defended by a length check I could not locate.

### Recommendation
Verify (and if missing, add) a check in `ProcessCentralDirEntry` that rejects or truncates any `file_name_length` greater than the destination buffer size (`PATH_MAX`) before copying central-directory filenames into fixed-size stack buffers, returning an error instead of writing past the buffer.

### Proof of Concept
Not confirmed — I was unable to retrieve the full body of `ProcessCentralDirEntry` (only found the 4 reference/match locations without being able to view its bounds-check logic) within the remaining tool budget. A `BuildIntegrationTestCase`/shell test would need to construct a hand-crafted ZIP central directory record with `file_name_length` > `PATH_MAX` (4096) and confirm whether `ijar`/`singlejar` crashes or corrupts memory when processing it — this must be validated against the actual `ProcessCentralDirEntry` source before treating this as a confirmed finding.

**Caveat:** Due to index/tool-call limits, I could not locate and read the full implementation of `ProcessCentralDirEntry` to confirm whether existing bounds-checking already prevents this. I recommend starting a full Devin session with filesystem access to inspect `third_party/ijar/zip.cc` in its entirety (particularly the `ProcessCentralDirEntry` function body) to confirm or refute this analog before treating it as a validated vulnerability.

### Citations

**File:** third_party/ijar/zip.cc (L140-142)
```text
  // Copy of the last filename entry - Null-terminated.
  char filename[PATH_MAX];
  // The external file attribute field
```

**File:** third_party/ijar/zip.cc (L302-310)
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
```

**File:** third_party/ijar/zip.cc (L357-364)
```text
  file_name_length_ = get_u2le(p);
  extra_field_length_ = get_u2le(p);

  if (EnsureRemaining(file_name_length_, "file_name") < 0) {
    return -1;
  }
  file_name_ = p;
  p += file_name_length_;
```
