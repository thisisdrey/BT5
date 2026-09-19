### Title
Missing bounds checks in `InputZipFile::ProcessCentralDirEntry` allow out-of-bounds reads when parsing attacker-supplied ZIP/JAR central directory entries - (File: third_party/ijar/zip.cc)

### Summary
`ijar`'s ZIP central-directory parser reads variable-length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) taken directly from an untrusted archive and advances/derefs the cursor `p` by those attacker-controlled lengths without ever checking that the resulting reads stay inside the mapped input buffer, unlike the sibling local-file-header parser which explicitly calls `EnsureRemaining()` before every variable-length read.

### Finding Description
`InputZipFile::ProcessLocalFileEntry` (third_party/ijar/zip.cc:332-418) calls `EnsureRemaining()` before reading `file_name_` and `extra_field_` so that an oversized attacker-declared length cannot push the cursor past the end of the mmap'd file: [1](#0-0) 

`InputZipFile::ProcessCentralDirEntry`, which parses each entry of the ZIP central directory, has no equivalent check at all. It reads `compressed size`, `uncompressed size`, `file_name_length`, `extra_field_length`, `file_comment_length`, `attr`, and `offset` directly from `p`, then does `memcpy(filename, p, len)` for up to `file_name_length` bytes and advances `p` by `file_name_length`, `extra_field_length`, and `file_comment_length` — all attacker-controlled 16-bit values (up to 65535 each) — before the next iteration reads from the new (possibly out-of-bounds) `p`: [2](#0-1) 

The only bound enforced anywhere near this path is a whole-region check in `FindZipCentralDirectory` that the *entire* central directory (`central_dir_offset + central_dir_size`) fits within the file: [3](#0-2) 

That check bounds the total central-directory region but does not validate any individual entry's `file_name_length`/`extra_field_length`/`file_comment_length` against the bytes actually remaining before the region's end. A crafted entry near the end of the (validly-sized) central directory can declare a `file_name_length` (or `extra_field_length`) that extends past `central_dir_offset + central_dir_size` — and even past the whole mapped file — causing the `memcpy` to read out-of-bounds heap memory into `filename[PATH_MAX]`, and causing `p`/`extra_p` to run past the buffer on subsequent field reads (`get_u2le`, `get_u4le`, `get_u8le`), which is exactly the class of bug fixed upstream in `ocfs2_check_dir_entry()` (missing per-entry bounds validation while iterating attacker-controlled directory-like records). `CalculateOutputLength()` and `ProcessNext()` both call `ProcessCentralDirEntry` in a loop, so a single malformed entry can also desynchronize the cursor for all subsequent entries. [4](#0-3) [5](#0-4) 

This code is compiled into `ijar`/`singlejar`-adjacent tooling that processes `.jar`/`.zip` files as part of the Java toolchain (e.g., building interface jars from dependency jars). A jar obtained from an untrusted origin (a build dependency fetched over the network, an untrusted-branch artifact, etc.) that a victim's build feeds into `ijar` reaches this parser with attacker-fully-controlled bytes.

### Impact Explanation
An out-of-bounds heap read can leak adjacent process memory into the copied `filename` buffer or corrupt control flow of the parsing loop (reading garbage lengths/offsets and potentially causing further OOB reads or a crash/DoS on subsequent iterations). Since `filename`/`attr`/`offset` derived from this parse feed the interface-jar generation logic, out-of-bounds data can leak into build outputs or cause the tool to crash while processing an untrusted dependency artifact.

### Likelihood Explanation
Likelihood is constrained by real-world reachability: this requires an attacker who can supply the actual bytes of a jar/zip that `ijar` consumes (e.g., a malicious dependency artifact), and the memory-safety bug class (missing length validation on a length-prefixed record while iterating a table of such records) is a well-established C++ parsing pitfall, mirrored exactly by the ocfs2 analog. I could not fully verify from the available index how `MappedInputFile`/`Open()` sizes/guards the mmap'd region (e.g., whether padding after the mapping makes small overreads benign) — this bounds the confidence of the finding.

### Recommendation
Add an `EnsureRemaining()`-style check in `ProcessCentralDirEntry` before reading `file_name_length` bytes into `filename`, before advancing `p` by `extra_field_length`, and before advancing `p` by `file_comment_length`, rejecting the archive with the existing `error()` path if any of these would run past the mapped file end (mirroring the checks already present in `ProcessLocalFileEntry`).

### Proof of Concept
A minimal repro (to be implemented as a `third_party/ijar/test` or shell test, similar to existing `third_party/ijar/test/zip_test.sh` path-traversal test) would: build a syntactically valid EOCD/central-directory region sized to pass the `central_dir_offset + central_dir_size > in_length` check, then patch the last central directory entry's `file_name_length` (or `extra_field_length`) field to a large value (e.g. `0xFFFF`) that exceeds the bytes actually remaining in the mapped file, and run `ijar`/the zipper tool over it, observing an out-of-bounds read (e.g., via ASan) or corrupted output instead of a clean parse error — analogous to `test_no_path_traversal` in third_party/ijar/test/zip_test.sh:277-283. [6](#0-5)

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

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```

**File:** third_party/ijar/test/zip_test.sh (L277-283)
```shellscript
function test_no_path_traversal() {
  local folder=$(mktemp -d ${TEST_TMPDIR}/output.XXXXXXXX)
  ! (cd $folder && $ZIPPER x $(dirname ${ZIPPER})/test/path_traversal_zip.jar)
  if [[ -e ${folder}/../ZIPPER_POC_OWNED ]]; then
    fail "Path traversal succeeded"
  fi
}
```
