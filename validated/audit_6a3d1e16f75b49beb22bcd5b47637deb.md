This confirms `ijar` (`third_party/ijar/zip.cc`) reads a `.jar` (any zip) file to generate an interface jar via `java_common.run_ijar` / `stamp_jar`, and the `jar` input is any `File`, including a jar fetched from an external, attacker-controlled source (e.g. `http_jar`, `java_import` pointing at a Maven artifact, or any dependency resolved by a build extension) — the invariant `sha256`/`integrity` checksum only pins the whole-file bytes, not the internal structure. I have enough evidence to write up the finding.

### Title
Out-of-bounds read while parsing central directory entries of an attacker-supplied jar/zip - (File: `third_party/ijar/zip.cc`)

### Summary
`ijar`, Bazel's interface-jar generator invoked via `java_common.run_ijar`/`stamp_jar` (and internally whenever Bazel builds a compile-jar for a `java_library`/`java_import` dependency), memory-maps an input `.jar`/`.zip` file and parses its ZIP central directory in `InputZipFile::ProcessCentralDirEntry`. Unlike the sibling local-file-header parser `InputZipFile::ProcessLocalFileEntry`, which calls `EnsureRemaining()` before consuming `file_name_length`/`extra_field_length` bytes [1](#0-0) , `ProcessCentralDirEntry` reads the 16-bit `file_name_length`, `extra_field_length`, and `file_comment_length` fields directly from attacker-controlled bytes and immediately advances the cursor and `memcpy`s from it with no bound check against the mapped file length [2](#0-1) .

### Finding Description
`FindZipCentralDirectory` only validates that `central_dir_offset + central_dir_size <= in_length` for the aggregate central directory [3](#0-2) ; it never validates the size or content of any individual entry inside that region. `InputZipFile::ProcessNext`/`ProcessCentralDirEntry` then walks entry-by-entry, trusting each entry's `file_name_length`, `extra_field_length`, and `file_comment_length` fields taken straight from the mapped bytes, and does:
```
memcpy(reinterpret_cast<void*>(filename), p, len);   // len derived from unchecked file_name_length
...
p += file_name_length;
...
p += extra_field_length;   // consumed in a while loop reading header_id/data_size pairs, also unchecked
...
p += file_comment_length;
``` [4](#0-3) 
A crafted entry can set these length fields so that `p` (and the `memcpy` source) run past the end of the memory-mapped input file, exactly analogous to CVE-2023-38407's stream-bound-less field parsing in FRR's `bgp_label.c`. The subsequent per-entry local-header dispatch in `ProcessNext` only bound-checks the 4-byte signature after the offset has already been (potentially) miscalculated [5](#0-4) .

### Impact Explanation
The mapped file is a `mmap`'d region (see `MappedInputFile` usage in `InputZipFile::Open`) [6](#0-5) ; reading past its end can dereference an unmapped page (crash / build failure, denial of the specific action) or — because `mmap` regions are page-granular — read adjacent heap/mapped memory into the `filename` buffer or `extra_p` walk, which can then be echoed into error messages or influence downstream `Accept`/`Process` decisions on the interface jar contents. This is an integrity/parsing-safety bug in an unprivileged-attacker-reachable file format parser used on any jar file Bazel is told to build an interface jar for.

### Likelihood Explanation
Any dependency resolved through a repository rule (e.g. `http_jar`, `http_file` + `java_import`, or a registry/BCR-served module providing pre-built jars) that a downstream `java_library`/`java_import` consumes is passed through `java_common.run_ijar`/`stamp_jar` to build compile jars, so a hostile origin server or mirror serving a byte-for-byte matching (checksum-satisfying) but structurally malicious ZIP central directory reaches this code with default flags on any current release, since `sha256`/`integrity` pinning only authenticates the file's bytes as a whole and does not validate internal ZIP structure invariants.

### Recommendation
Add explicit bounds checks (mirroring `EnsureRemaining` used in `ProcessLocalFileEntry`) in `ProcessCentralDirEntry` before consuming `file_name_length`, `extra_field_length`, and `file_comment_length`, and validate that each field, plus the fixed 46-byte central-directory-header prefix, stays within `[bytes, bytes + in_length)` before advancing `p` or calling `memcpy`.

### Proof of Concept
A `BuildIntegrationTestCase`/shell test analogous to `src/test/shell/bazel/*_test.sh` archive tests can construct a minimal valid ZIP (matching EOCD/central-dir-size/offset invariants checked by `FindZipCentralDirectory`) whose single central directory entry declares `file_name_length = 0xFFFF` while the actual mapped buffer ends a few bytes later, then invoke `ijar <crafted.jar> out.jar` (or exercise it via `java_common.run_ijar` on a `java_import` pointing at this crafted jar) and observe a segfault/ASAN heap-buffer-overflow report from the `memcpy` in `ProcessCentralDirEntry`, matching the existing `zip_headers_test.cc` structure for constructing test fixtures. [7](#0-6)

### Citations

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

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
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
