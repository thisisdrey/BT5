## Analog Vulnerability Found

### Title
Unbounded out-of-bounds read via unchecked central-directory field lengths in `ijar`'s ZIP/JAR parser causing crash on crafted input — (File: `third_party/ijar/zip.cc`)

### Summary
The libsndfile CVE is a segmentation fault caused by trusting length/size fields taken from an attacker-supplied file and using them to copy/advance through a buffer without validating they stay within the mapped input. Bazel's `ijar` tool (`third_party/ijar/zip.cc`), which every Java build target's jars pass through (`java_common.run_ijar`, `interface jar` generation for `java_library`/`java_import`), has the same class of defect in `InputZipFile::ProcessCentralDirEntry`.

### Finding Description
`InputZipFile::ProcessCentralDirEntry` reads a Central Directory Header (CDH) from the memory-mapped input jar/zip: [1](#0-0) 

Unlike `ProcessLocalFileEntry`, which calls `EnsureRemaining()` before consuming `file_name_length_`/`extra_field_length_` bytes ( [2](#0-1) ), `ProcessCentralDirEntry` performs **no bounds check at all** on `file_name_length`, `extra_field_length`, or `file_comment_length` read straight from attacker-controlled bytes: `p` is advanced by these fully attacker-controlled 16-bit values with no comparison against the mapped file's end.

The extra-field walking loop compounds the problem: [3](#0-2) 

`while (extra_p != p)` only terminates on exact equality. If a crafted `data_size` causes `extra_p` to overshoot `p` (e.g., a `data_size` value larger than the actual remaining extra-field bytes), the loop never satisfies `extra_p == p` and continues reading 2+2-byte header/size fields and up to 8 bytes via `get_u8le(extra)` indefinitely past the mapped region. `FindZipCentralDirectory` only validates the aggregate central-directory size against the file length ( [4](#0-3) ); it never validates that a single entry's variable-length fields stay inside that region.

### Impact Explanation
A crafted jar/zip whose central directory advertises oversized `extra_field_length`/`file_comment_length` (or a malformed ZIP64 extra field) drives the parser cursor past the mmap'd input buffer. Reading unmapped memory triggers `SIGSEGV`, directly mirroring the "segmentation violation (with write memory access)" impact described for CVE-2017-7741's `flac_buffer_copy()`. Because `ijar` runs as part of ordinary Java build actions on any dependency jar the build consumes — including third-party jars fetched from a URL/registry and pinned by a declared `sha256` (the pinned hash simply certifies "this is the attacker's malicious byte stream", it does not protect the parser from malformed internal structure) — an untrusted jar can crash the Bazel build worker.

### Likelihood Explanation
High reachability: any `java_library`/`java_import` target or `java_common.run_ijar` call invokes ijar on every jar dependency. An attacker who controls the content of one dependency jar (a hostile mirror, a malicious registry artifact, or a jar committed to an untrusted branch that CI builds) can trivially craft a ZIP central directory entry with an oversized length field. No credential or MITM is required, and the crafted file's own recorded checksum can be honestly pinned by a victim without preventing this.

### Recommendation
Add explicit bounds checks in `InputZipFile::ProcessCentralDirEntry`, symmetric to `ProcessLocalFileEntry`'s `EnsureRemaining()` calls, before reading `file_name_length`, `extra_field_length`, `file_comment_length`, and before advancing `p`/`extra_p` by these values. In the ZIP64 extra-field loop, bound the walk by an explicit end pointer check (`extra_p < p`, and cap `extra_p + data_size <= p`) rather than relying on exact pointer equality, and validate `data_size >= 8` before calling `get_u8le`.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` style reproduction:
1. Construct a minimal ZIP file containing a valid End-Of-Central-Directory record and one Central Directory Header entry whose `extra_field_length` (or `file_comment_length`) field is set larger than the number of bytes actually present before the EOCD record, while keeping the aggregate `central_dir_size`/`central_dir_offset` check in `FindZipCentralDirectory` satisfied (e.g., by mismatching an individual entry's length against the reported end-of-central-directory position).
2. Feed this file as the `jar` argument to the `ijar` binary (or via `java_common.run_ijar` on a `java_import`/`java_library` target depending on this crafted jar).
3. Observe that `ijar` crashes with `SIGSEGV` (or ASAN reports heap-buffer-overflow/out-of-bounds read) instead of returning a clean parse error, analogous to the existing `zip_headers_test.cc`/`InputJarScanEntries` tests that exercise `CDH`/`LH` parsing but do not cover malformed/oversized length fields.

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

**File:** third_party/ijar/zip.cc (L526-542)
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
  }
```

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```
