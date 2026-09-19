### Title
Heap buffer over-read in ijar's ZIP central directory parser via unchecked length fields - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` reads a central directory entry's `file_name_length`, `extra_field_length`, and `file_comment_length` fields and advances the cursor `p` by those attacker-controlled values, and reads/`memcpy`s from `p`, without ever validating that `p` (or `p + file_name_length`, etc.) stays within the bounds of the memory-mapped input file. This mirrors the GPMF-parser class of bug (CVE-2018-13009): a length/offset value taken directly from untrusted, attacker-supplied data is trusted to advance a cursor and is not checked against the real remaining buffer size before the next read, producing a heap-based buffer over-read.

### Finding Description
`ProcessCentralDirEntry` reads: [1](#0-0) 

Note there is no call to `EnsureRemaining()` anywhere in this function, unlike its sibling `ProcessLocalFileEntry`, which explicitly guards every variable-length read: [2](#0-1) [3](#0-2) 

In `ProcessCentralDirEntry`, `file_name_length`, `extra_field_length`, and `file_comment_length` are 16-bit values fully controlled by the attacker-supplied ZIP central directory. The function immediately:
- `memcpy`s `file_name_length` bytes from `p` into a fixed `filename[PATH_MAX]` buffer (bounded by `filename_size`, but the *source* pointer `p` is never checked against the mapped file's end before the copy),
- advances `p` by `file_name_length`, then by `extra_field_length`, and loops through "extra fields" reading `get_u2le`/`get_u8le` from `extra_p`, and finally advances `p` by `file_comment_length` — all without bounds checks against the mapped input buffer end.

This function is called from `InputZipFile::ProcessNext()` and `CalculateOutputLength()`, which iterate the central directory of any ZIP/JAR file opened via `InputZipFile::Open()`, which simply memory-maps the file with `MappedInputFile`: [4](#0-3) [5](#0-4) 

A crafted central directory entry with a `file_name_length`/`extra_field_length`/`file_comment_length` that runs past the end of the mapped file (or a manipulated `local_header_offset`/central-directory size relationship) causes `get_u4le`/`get_u2le`/`memcpy` to read from unmapped memory adjacent to the mmap'd region — a heap/mmap-region over-read, directly analogous to `GPMF_Next`'s missing bound check against `buffer_size_longs` before consuming a nested/END key.

The comment above the function even asserts a false safety invariant: "Note that the central directory is always followed by another data structure that has a signature, so parsing it this way is safe" — this assumption does not hold for a maliciously truncated or crafted file where the declared lengths intentionally overrun the actual mapped extent.

### Impact Explanation
ijar's ZIP reader (`third_party/ijar/zip.cc`) is the parser Bazel uses to build interface jars (`ijar`) from any `.jar` file used as a Java compile-time dependency, including jars originating from external repositories (e.g., `java_import`/Maven-fetched jars via `http_jar`/`http_file`/`maven_install`, whose bytes are supplied by an attacker-controlled or attacker-served origin and whose sha256 only proves the bytes match what was originally hashed, not that the ZIP structure is well-formed). Feeding such a crafted jar into ijar during a normal build triggers an out-of-bounds read that can crash the Bazel build process (worker) or, depending on adjacent heap/mmap layout, leak adjacent memory contents into copied filenames/central-directory fields that end up embedded in build outputs. This is a genuine memory-safety violation in Bazel's own code reachable purely by supplying a hostile file that a build consumes — a pinned sha256/integrity check does not stop it because the malicious bytes are exactly what was hashed.

### Likelihood Explanation
Any project that depends on a `java_import`, `aar_import`, or similarly packaged pre-built artifact fetched from an external, potentially attacker-controlled source (mirror, registry, or unpinned re-fetch scenario) will have ijar invoked automatically on that artifact as part of ordinary Java build actions. No special build flags are required — this is default behavior for any Java target with a pre-compiled jar dependency.

### Recommendation
Add explicit bounds checks (analogous to `EnsureRemaining`) in `ProcessCentralDirEntry` before every variable-length read/skip (`file_name_length`, `extra_field_length`, `file_comment_length`, and each per-field `data_size` in the extra-field loop), rejecting/erroring out the same way `ProcessLocalFileEntry` already does, rather than relying on the informal assumption that a signature will always immediately follow.

### Proof of Concept
Construct a malformed ZIP/JAR whose End-of-Central-Directory record points to a central directory near the end of the mapped file, and whose single central directory entry declares `file_name_length`/`extra_field_length`/`file_comment_length` values large enough that `p` (and the source of the `memcpy`) advances past the actual end of the mapped file region. Running `ijar`/`zipper` (or any Bazel build that processes this jar as a `java_import`) over this file, e.g. via a `BuildIntegrationTestCase`/`src/test/shell/bazel` script invoking the `ijar`/`zipper` binary or `ProcessAll()` directly, causes `get_u4le`/`get_u2le`/`memcpy` in `ProcessCentralDirEntry` to read past the mapped buffer, observable as a crash (SIGSEGV/ASAN heap-buffer-overflow) instead of a clean parse error such as those emitted by `EnsureRemaining` in `ProcessLocalFileEntry`.

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

**File:** third_party/ijar/zip.cc (L358-370)
```text
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

**File:** third_party/ijar/zip.cc (L814-846)
```text
bool InputZipFile::Open() {
  MappedInputFile* input_file = new MappedInputFile(filename_);
  if (!input_file->Opened()) {
    snprintf(errmsg, sizeof(errmsg), "%s", input_file->Error());
    delete input_file;
    return false;
  }

  void *zipdata_in = input_file->Buffer();
  u8 central_dir_offset;
  const u1 *central_dir = NULL;

  if (!devtools_ijar::FindZipCentralDirectory(
          static_cast<const u1*>(zipdata_in), input_file->Length(),
          &central_dir_offset, &central_dir)) {
    errno = EIO;  // we don't really have a good error number
    error("Cannot find central directory");
    delete input_file;
    return false;
  }
  const u1 *zipdata_start = static_cast<const u1*>(zipdata_in);
  in_offset_ = - static_cast<off_t>(zipdata_start
                                    + central_dir_offset
                                    - central_dir);

  input_file_ = input_file;
  zipdata_in_ = zipdata_start;
  central_dir_ = central_dir;
  central_dir_current_ = central_dir;
  p = zipdata_in_ + in_offset_;
  errmsg[0] = 0;
  return true;
}
```
