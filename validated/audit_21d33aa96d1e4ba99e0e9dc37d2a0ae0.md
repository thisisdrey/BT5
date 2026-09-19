### Title
Out-of-bounds read in ijar's ZIP central-directory parser via unchecked length fields - (File: `third_party/ijar/zip.cc`)

### Summary
Bazel's native `ijar`/`zipper` ZIP reader parses central-directory records with attacker-controlled 16-bit length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) and a ZIP64 extra-field sub-record length (`data_size`) without ever checking these values against the number of bytes actually remaining in the memory-mapped input file. This mirrors the CVE-2023-37444 bug class (unbounded length-prefixed fields in a var/record definition section driving a native-code out-of-bounds read).

### Finding Description
`InputZipFile::ProcessCentralDirEntry` in [1](#0-0)  reads a central-directory entry directly from the mmap'd buffer:

- It reads `file_name_length`, `extra_field_length`, and `file_comment_length` via `get_u2le` and then advances the cursor `p` by those raw values with no check that `p` stays within `zipdata_in_ .. zipdata_in_ + length_` [2](#0-1) .
- It then walks the "extra field" as a sequence of TLV records, reading a 2-byte `header_id` and 2-byte `data_size` per iteration and advancing `extra_p += data_size`, again with no bound relative to the mapped file length; the loop terminates only when `extra_p == p`, which an attacker can prevent by choosing a `data_size` that makes `extra_p` overshoot `p` [3](#0-2) .
- Finally `p += file_comment_length` moves the cursor further with no bound check [4](#0-3) .

This is unlike the local-file-header parser `ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before consuming `file_name_length_`/`extra_field_length_` [5](#0-4) . `ProcessCentralDirEntry` and its caller `CalculateOutputLength` (used to size the interface-jar output) have no equivalent guard [6](#0-5) .

The input file is mapped with exactly `length` bytes and no guard page, per `MappedInputFile::MappedInputFile` in `mapped_file_unix.cc`: `mmap(NULL, length, PROT_READ, MAP_PRIVATE, fd, 0)` [7](#0-6) . Note that Bazel's authors were aware of exactly this class of bug for the *output* mapping and deliberately over-allocated by a page there ("Ensure that any buffer overflow in JarStripper will result in SIGSEGV or SIGBUS by over-allocating beyond the end of the file") [8](#0-7)  — but no such protection (nor bounds checking) exists for the *input* mapping used by the central-directory parser.

### Impact Explanation
A crafted `.jar`/`.zip` file with an oversized `extra_field_length`/`file_comment_length` or a ZIP64 extra-field `data_size` that pushes the read cursor past `zipdata_in_ + length_` causes `ijar` (and the `zipper`/interface-jar generation path that Bazel invokes as part of Java compilation) to read heap memory outside the mapped file region. This can crash the build worker (denial of service against the build) or, depending on process layout, leak adjacent heap bytes into subsequent parsing decisions (e.g. into `filename[PATH_MAX]`, `attr`, size fields) that influence generated output — an information-disclosure/robustness issue directly analogous to the GTKWave OOB read in unauthenticated length-prefixed record parsing.

Because a checksum (`sha256`/integrity hash on the downloaded archive) verifies only that the bytes match what the attacker published — not that the ZIP/JAR is structurally well-formed — a hostile origin serving a jar dependency (e.g. via `http_jar`, `http_archive`, or a Maven-style download consumed by `java_import`/interface-jar generation) can pin an integrity hash that matches its own malicious bytes and still trigger this native out-of-bounds read when Bazel processes the file with `ijar`.

### Likelihood Explanation
Any build depending on an externally-fetched `.jar` that gets run through `ijar`'s interface-jar generation (a very common path for Java rules, both for `java_import`-style precompiled jars and for output jars) exercises this exact parsing code. No `http_archive`/download integrity mechanism validates internal ZIP structure, so the checksum pinning provides no protection against this class of malformed input.

### Recommendation
Add `EnsureRemaining()`-style bounds checks in `InputZipFile::ProcessCentralDirEntry` before consuming `file_name_length`, `extra_field_length`, `file_comment_length`, and before each ZIP64 extra-field `header_id`/`data_size` read, validating the cursor against `input_file_->Length()`/`zipdata_in_ + length_` exactly as `ProcessLocalFileEntry` already does, and bail out with an error instead of advancing past the mapped file end.

### Proof of Concept
Construct a minimal ZIP file whose End-Of-Central-Directory record points to a single Central Directory File Header located near the very end of the file, and set that header's `extra field length` (and/or a ZIP64 extra-field `data_size`) to a large value (e.g. `0xFFFE`) so that `p`/`extra_p` in `ProcessCentralDirEntry` is advanced past `zipdata_in_ + length_`. Running `ijar <crafted.jar> <out.jar>` (exercising `InputZipFile::Open`/`CalculateOutputLength`/`ProcessNext`) triggers reads past the end of the `mmap`'d region, observable as a SIGSEGV/SIGBUS under ASan or a crash near the end of a page-aligned file size, which can be encoded as a `src/test/shell/bazel` integration test invoking the `ijar` binary directly on such a crafted archive and asserting non-crash/graceful-error behavior after the fix.

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

**File:** third_party/ijar/mapped_file_unix.cc (L47-65)
```text
  off_t length = lseek(fd, 0, SEEK_END);
  if (length < 0) {
    snprintf(errmsg, MAX_ERROR, "lseek(): %s", strerror(errno));
    errmsg_ = errmsg;
    return;
  }

  void* buffer = mmap(NULL, length, PROT_READ, MAP_PRIVATE, fd, 0);
  if (buffer == MAP_FAILED) {
    snprintf(errmsg, MAX_ERROR, "mmap(): %s", strerror(errno));
    errmsg_ = errmsg;
    return;
  }

  impl_ = new MappedInputFileImpl();
  impl_->fd_ = fd;
  buffer_ = reinterpret_cast<u1*>(buffer);
  length_ = length;
  opened_ = true;
```

**File:** third_party/ijar/mapped_file_unix.cc (L106-112)
```text
  // Ensure that any buffer overflow in JarStripper will result in
  // SIGSEGV or SIGBUS by over-allocating beyond the end of the file.
  size_t mmap_length =
      std::min(static_cast<size_t>(estimated_size + sysconf(_SC_PAGESIZE)),
               std::numeric_limits<size_t>::max());
  void* mapped =
      mmap(NULL, mmap_length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
```
