## Finding: Out-of-bounds read in ijar's ZIP central-directory parser

Bazel's `ijar` tool (used by `java_common.run_ijar`, invoked automatically to build interface jars for every Java jar including `java_import` targets that reference locally-mirrored, hash-unpinned, or attacker-served jars) parses ZIP central-directory entries in `InputZipFile::ProcessCentralDirEntry` without validating attacker-controlled length fields against the actual bounds of the mapped input file. [1](#0-0) 

### Finding Description
`ProcessCentralDirEntry` reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly from the untrusted central-directory record and immediately uses `file_name_length` in a `memcpy` call, then advances the cursor `p` by all three attacker-controlled lengths, before any check that these values are consistent with the remaining bytes in the memory-mapped file: [2](#0-1) 

This is structurally identical to the referenced CVE-2023-37281 pattern: a length field taken from attacker-controlled input is trusted and used to bound a `memcpy`/pointer-advance without first confirming that many bytes actually remain in the buffer.

By contrast, the sibling function `InputZipFile::ProcessLocalFileEntry`, which parses the local file header, does call `EnsureRemaining()` before consuming `file_name_length_`/`extra_field_length_` bytes: [3](#0-2) 

No equivalent `EnsureRemaining`-style check exists in `ProcessCentralDirEntry`. The only bound enforced earlier is a whole-central-directory-blob check in `FindZipCentralDirectory`: [4](#0-3) 

That check only ensures `central_dir_offset + central_dir_size <= in_length` for the *aggregate* central directory; it does nothing to prevent a single entry's declared `file_name_length`/`extra_field_length`/`file_comment_length` from being large enough to push the parse cursor `p` past `bytes + in_length` (i.e., past the end of the memory-mapped file), especially for an entry placed near the end of the central directory blob.

### Impact Explanation
When `p` walks past the mapped file's end and `memcpy(filename, p, len)` executes, the read touches unmapped or adjacent-heap memory. Depending on layout, this can:
- Crash the build (SIGSEGV) reading past an mmap'd region boundary — a low-severity availability issue.
- Copy adjacent heap bytes into the `filename` buffer, which is subsequently passed to `processor->Accept(filename, attr)` and used in the interface jar's own directory entries/diagnostics, potentially leaking adjacent process memory content into build artifacts or error output.

This mirrors the CVSS 5.3 (network-adjacent, no privileges, low confidentiality impact) profile of the analog CVE.

### Likelihood Explanation
`ijar` runs on the jars of `java_import`/`java_library` targets, including third-party or downloaded jars, whenever a compile jar is generated. An attacker who controls a URL from which such a jar is fetched (e.g. a `java_import` whose `jars` label resolves to an `http_file`/`http_jar` without a correctly enforced `sha256`, or a compromised mirror) can craft a malformed but structurally-plausible ZIP with an inflated central-directory-entry length field and trigger this path deterministically. Because `ijar` is invoked automatically as part of the standard Java build graph, no special build configuration is needed beyond depending on the malicious jar.

### Recommendation
Add bounds validation in `ProcessCentralDirEntry` analogous to `EnsureRemaining()` in `ProcessLocalFileEntry`: before advancing `p` by `file_name_length`, `extra_field_length`, or `file_comment_length`, verify that `p + <field length> <= bytes + in_length` and fail with a diagnostic (as already done for corrupt/undersized files) instead of reading past the buffer.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` style repro would: 1) hand-construct a ZIP file whose EOCD/central-directory bookkeeping fields (`central_dir_offset`, `central_dir_size`) are internally consistent and pass the `FindZipCentralDirectory` aggregate check, but where the final central-directory entry declares a `file_name_length` (or `extra_field_length`/`file_comment_length`) large enough that `p + length` exceeds `bytes + in_length`; 2) run `ijar`/`java_common.run_ijar` (or invoke `third_party/ijar/zip_main.cc`'s extractor directly) on that file under ASan; 3) observe the ASan heap/global-buffer-overflow (or SEGV) triggered inside `memcpy` in `ProcessCentralDirEntry` at [5](#0-4) , confirming the read occurs beyond the mapped input file's extent.

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

**File:** third_party/ijar/zip.cc (L493-525)
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
```

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```
