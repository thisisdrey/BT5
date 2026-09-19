### Title
Out-of-bounds heap read parsing attacker-controlled ZIP central directory in `ijar` — (File: third_party/ijar/zip.cc)

### Summary
`ijar`'s ZIP/JAR reader (`InputZipFile::ProcessCentralDirEntry` and `InputZipFile::CalculateOutputLength`) parses the ZIP central directory of a downloaded/fetched JAR by trusting attacker-controlled 16-bit length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) and the ZIP64 extra-field `data_size` sub-fields, without ever checking that the resulting pointer advances stay within the memory-mapped input file. This is the same bug class as `decode_preauth_ctxt()`: a length field taken from untrusted input is used to index/advance into a buffer without confirming it stays inside the buffer boundary.

### Finding Description
`ProcessCentralDirEntry` reads the fixed 46-byte central directory header fields and then unconditionally trusts the three attacker-supplied 16-bit lengths to advance the cursor and to `memcpy` the file name: [1](#0-0) 

Unlike `InputZipFile::ProcessLocalFileEntry`, which calls `EnsureRemaining()` before consuming `file_name_length_`/`extra_field_length_`: [2](#0-1) 

`ProcessCentralDirEntry` has **no equivalent bounds check** against `input_file_->Length()` (or against the known extent of the central directory) before it:
- `memcpy`s `file_name_length` bytes from `p` into `filename`,
- advances `p` by `file_name_length` and then by `extra_field_length`,
- and, inside the extra-field walk, reads a 2-byte `header_id`/`data_size` pair and advances by the attacker-controlled `data_size` with no check that `extra_p + 4 <= p` or `extra + data_size <= p`: [3](#0-2) 

If `header_id == ZIP64_EXTRA_FIELD_TAG`, `get_u8le(extra)` is then used to read 8-byte quantities directly from that unchecked `extra` pointer: [4](#0-3) 

Because `zipdata_in_`/`central_dir_` point into a `mmap`'d region (`MappedInputFile`), a crafted `file_name_length`, `extra_field_length`, or ZIP64 `data_size` value can push `p`/`extra_p` past the end of the mapped file (or of the central directory region), causing reads of adjacent heap/mapped memory. `InputZipFile::CalculateOutputLength` drives this same unchecked function in a loop over the whole central directory with attacker-supplied data: [5](#0-4) 

### Impact Explanation
`ijar` is invoked by Bazel's build rules (e.g., to build interface jars from Java dependencies) on JAR files that originate from external, attacker-influenced sources (`http_jar`, `http_archive`/`maven_install`-fetched artifacts, or any zip/jar pulled in through a repository rule). An attacker who controls the content served at a dependency URL only needs to publish a JAR with a malformed central directory (oversized `file_name_length`, `extra_field_length`, or ZIP64 extra-field `data_size`) to make `ijar` read outside the bounds of the mapped input buffer. Depending on memory layout this can: (a) read adjacent heap memory into the `filename` buffer via `memcpy`, whose contents subsequently flow into the generated interface jar / output artifacts (confidentiality impact — leaking unrelated process memory into build outputs that may be shared via caches or committed), or (b) crash the process by touching an unmapped page past the `mmap` region.

### Likelihood Explanation
Reachable by any unprivileged attacker who can serve the archive bytes fetched by a repository rule (dependency URL, registry artifact, or a JAR checked into an untrusted branch that CI subsequently builds with `ijar`). No cooperation from checksum verification is required to reach this parser, since the vulnerability lives in the *post-download* processing of the archive's ZIP structure, not in the transport/integrity layer — a JAR that matches its declared `sha256`/lockfile digest byte-for-byte can still contain a malformed central directory.

### Recommendation
Add explicit bounds checks (mirroring the `EnsureRemaining`/`ExtraField::find` style bounds checks already used elsewhere in the codebase, e.g. in `src/tools/singlejar/zip_headers.h`'s `ExtraField::find`) to `ProcessCentralDirEntry`:
- Verify `p + file_name_length + extra_field_length + file_comment_length` does not exceed the mapped file end (or the known central directory end) before advancing/copying.
- Verify `extra_p + sizeof(header_id) + sizeof(data_size) <= p` and `extra_p + data_size <= p` before reading each extra-field entry, exactly as `src/tools/singlejar/zip_headers.h`'s `ExtraField::find` already does.

### Proof of Concept
Construct a minimal ZIP whose single central directory entry declares `extra_field_length = 0xFFFF` while the actual file only contains a few trailing bytes after the central directory header (i.e., the mapped file ends well before `p + extra_field_length`), then run `ijar` (or any Bazel build rule that internally invokes `ijar`, such as building a `java_library` with this JAR as a dependency) against it. Under ASan/valgrind, this reproduces as a heap-buffer-overflow / out-of-bounds read inside `InputZipFile::ProcessCentralDirEntry`; this can be codified as a `src/test/shell/bazel` or `googletest`-based test in `third_party/ijar/` that feeds a hand-crafted malformed central directory into `ZipExtractor::Create`/`ProcessAll` and asserts no OOB access occurs (e.g., via ASan build) instead of a clean parse failure.

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

**File:** third_party/ijar/zip.cc (L507-543)
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
  p += file_comment_length;
```

**File:** third_party/ijar/zip.cc (L550-574)
```text
u8 InputZipFile::CalculateOutputLength() {
  const u1* current = central_dir_;

  u8 compressed_size = 0;
  u8 uncompressed_size = 0;
  u8 skipped_compressed_size = 0;
  u4 attr;
  u8 offset;
  char filename[PATH_MAX];

  while (true) {
    u8 file_compressed, file_uncompressed;
    if (!ProcessCentralDirEntry(current,
                                &file_compressed, &file_uncompressed,
                                filename, PATH_MAX, &attr, &offset)) {
      break;
    }

    if (processor->Accept(filename, attr)) {
      compressed_size += (u8) file_compressed;
      uncompressed_size += (u8) file_uncompressed;
    } else {
      skipped_compressed_size += file_compressed;
    }
  }
```
