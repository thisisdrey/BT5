### Title
Unbounded read in ijar's ZIP central-directory parser can walk past the mapped file on a crafted `.jar`/`.aar`/`.zip` - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` reads attacker-controlled length fields (`file_name_length`, `extra_field_length`, `file_comment_length`, and per-extra-field `data_size`) from the ZIP central directory and advances/dereferences the cursor `p`/`extra_p` by those values without any bounds check against the mapped input file's actual size, unlike the local-file-entry path (`ProcessLocalFileEntry`) which explicitly calls `EnsureRemaining()` before every variable-length read.

### Finding Description
`ProcessCentralDirEntry` in [1](#0-0)  parses each central directory record directly from the memory-mapped ZIP/JAR file. After validating only the 4-byte signature, it reads `file_name_length`, `extra_field_length`, and `file_comment_length` as raw `u2` values from the buffer and then:
- `memcpy`s `file_name_length` bytes from `p` into a fixed caller buffer [2](#0-1) 
- advances `p` by `file_name_length`, then by `extra_field_length` [3](#0-2) 
- walks the "extra fields" region using an internal `data_size` field read from the same untrusted buffer, with no check that `extra_p + data_size` stays inside the extra-field region or the mapped file [4](#0-3) 
- advances `p` again by `file_comment_length` [5](#0-4) 

None of these reads/advances are preceded by an `EnsureRemaining()`-style check against `input_file_->Length()`. Contrast this with `InputZipFile::ProcessLocalFileEntry`, which does call `EnsureRemaining(file_name_length_, "file_name")` and `EnsureRemaining(extra_field_length_, "extra_field")` before consuming those fields [6](#0-5) , and `EnsureRemaining` itself is defined to bound-check against the mapped file length [7](#0-6) . The central-directory path has no equivalent guard, so a crafted length/`data_size` value can push `p`/`extra_p` past the end of the `mmap`ed region, causing an out-of-bounds read (or a `memcpy` from unmapped memory) when processing the next iteration — the same bug class as CVE-2017-14261 (an unchecked size/count field in a media container atom driving an out-of-bounds read).

This function is invoked from `InputZipFile::ProcessNext`, the driver used by ijar's `zipper`/`ijar` tools when generating interface jars or listing/extracting ZIP archives; the input can be an externally supplied `.jar`/`.aar`/`.zip` file (e.g., a jar produced by a third-party dependency that Bazel's Java rules invoke `ijar`/`zipper` on) whose central directory is fully attacker-controlled bytes.

### Impact Explanation
An out-of-bounds read past the end of a memory-mapped file typically causes a segfault (crash of the ijar/zipper subprocess used during a build), and in principle could leak adjacent heap/mmap memory content into the copied filename buffer depending on layout. This matches CVE-2017-14261's class ("Read Memory Access Violation" from an unchecked size field) rather than a memory-corruption RCE. It is a build-tool crash / potential minor information disclosure, not integrity bypass of a checksum, sandbox escape, or credential exfiltration.

### Likelihood Explanation
Reaching this code requires a build to run `ijar`/`zipper` over a ZIP/JAR file whose bytes are fully attacker-influenced (e.g., a malicious dependency jar). No checksum on a JAR's *internal structure* would stop malformed length fields — SHA/integrity checks in `http_archive`/`http_jar` verify the whole file's hash, not that its ZIP structure is well-formed, so an attacker publishing a byte-for-byte crafted (but hash-matching-if-they-control-the-URL) jar can still trigger this on first parse.

### Recommendation
Add `EnsureRemaining()`-equivalent bounds checks in `ProcessCentralDirEntry` before consuming `file_name_length`, `extra_field_length`, `file_comment_length`, and before each extra-field `data_size` advance, mirroring the existing guards in `ProcessLocalFileEntry`. Reject the entry (return `false`/error) rather than blindly advancing the cursor when any of these lengths would run past `input_file_->Length()`.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell` reproduction would need to:
1. Craft a minimal ZIP file whose central directory entry declares `file_name_length`/`extra_field_length`/`file_comment_length` (or a nested extra-field `data_size`) values that exceed the remaining bytes in the file (e.g., set `file_name_length = 0xFFFF` in a file whose central directory ends a few bytes later).
2. Invoke `ijar`'s `zipper`/`ijar` binary (or the `InputZipFile`/`ProcessAll` API in `third_party/ijar/zip.cc`) directly on this crafted file, e.g. through the existing `third_party/ijar/zip_main.cc` `extract`/list path.
3. Observe a crash (SIGSEGV) or read of out-of-bounds/uninitialized memory instead of a clean "corrupt archive" error, verifying the missing bounds check compared to the analogous `ProcessLocalFileEntry`/`EnsureRemaining` path.

I was not able to fully trace, within the indexed content, the exact Bazel Java-rule code path that invokes the native `ijar`/`zipper` binary on externally downloaded jars (e.g., via `java_import`/Maven-resolved deps) to confirm end-to-end reachability from an unprivileged network attacker in this specific repo snapshot — the index only surfaced Starlark doc references to `run_ijar`. Confirming that wiring would require a full checkout via a Devin session.

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
