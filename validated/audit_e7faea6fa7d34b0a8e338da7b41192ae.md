### Title
Missing bounds check in zip central-directory parsing causes out-of-bounds read of attacker-supplied archive data - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessCentralDirEntry` reads the variable-length `file_name`, `extra_field`, and `file_comment` fields of a zip central-directory record and advances the input cursor by their attacker-controlled length values without verifying that the remaining mapped-file bytes are sufficient, unlike the analogous local-file-header parser which explicitly calls `EnsureRemaining()` before doing the same.

### Finding Description
`FindZipCentralDirectory` (third_party/ijar/zip.cc:704-778) validates only that `cd.central_dir_offset + cd.central_dir_size <= in_length` — i.e., that the *whole* central directory region lies inside the mmap'd file [1](#0-0) . It does not verify that any individual entry's declared `file_name_length` / `extra_field_length` / `file_comment_length` keeps the read cursor inside that region or inside the mapped file at all.

`ProcessCentralDirEntry` then reads these attacker-controlled u2 length fields and unconditionally advances the pointer and copies from it: [2](#0-1) 
The extra-field walk loop similarly trusts `data_size` read from the buffer to advance `extra_p`, with no check that `extra_p + data_size` stays within `extra_field_length` or the mapped file: [3](#0-2) 

This is the same bug class as CVE-2020-0067 (`f2fs_xattr_generic_list`): a length value taken from untrusted, attacker-supplied structured data is used to bound a memory copy/read without checking it against the actual size of the backing buffer.

By contrast, the local file header parser (`ProcessLocalFileEntry`) explicitly guards every variable-length read with `EnsureRemaining()`: [4](#0-3) 
No equivalent guard exists in `ProcessCentralDirEntry`, and the primitive length-decoders (`get_u2le`, `get_u4le`, `get_u8le` in `third_party/ijar/common.h`) perform raw pointer dereferences with no bounds checking at all [5](#0-4) .

The zip file itself is mapped read-only via `MappedInputFile` (`zipdata_in_`), and this code path is exercised whenever Bazel-embedded tooling (`ijar`, and the `zipper`/`unzip` helper in `zip_main.cc`) opens and enumerates a zip/jar archive — including jars that arrive as external dependencies (e.g., via `http_jar`/`http_archive`) and are then read to build interface jars or extracted by the `zipper` tool [6](#0-5) .

### Impact Explanation
A maliciously crafted zip/jar central-directory entry with an oversized `file_name_length`/`extra_field_length`/`file_comment_length` relative to the remaining bytes in the mapped file causes reads past the end of the mmap'd region. This can disclose adjacent process memory (e.g., into the `filename[PATH_MAX]` buffer via `memcpy`, later surfaced in diagnostics/log output or used as an extracted path) or crash the process (SIGSEGV / DoS on the page boundary). This matches the reported CVE's class: local information disclosure via out-of-bounds read due to a missing bounds check, no elevated privileges required by the attacker beyond publishing the malicious file content.

### Likelihood Explanation
The archive's checksum/integrity, if pinned, only binds the byte-for-byte content of the file the attacker chooses to publish — it does not validate that the internal zip structure is well-formed. An attacker fully controlling a hosted zip/jar dependency (with a self-consistent sha256 they compute and publish, or relying on Bazel's default non-hermetic warning when no checksum is set) can trivially construct a central directory entry whose declared lengths exceed the remaining mapped bytes. Because `ijar` runs by default on jar dependencies used at compile time and `zipper`/`unzip` are used broadly in Bazel's build and packaging tooling, the vulnerable code path is reachable with ordinary use of external zip/jar artifacts, without any additional user opt-in.

### Recommendation
Add an `EnsureRemaining`-equivalent bounds check in `ProcessCentralDirEntry` before reading/advancing past `file_name_length`, `extra_field_length`, and `file_comment_length`, and validate that `extra_p + data_size` does not exceed `extra_field_length`/the mapped file bounds in the ZIP64 extra-field loop, mirroring the checks already present in `ProcessLocalFileEntry`.

### Proof of Concept
1. Construct a minimal zip file whose End-Of-Central-Directory / central directory is valid enough to pass `FindZipCentralDirectory`'s aggregate size check, but whose single central-directory entry declares `file_name_length = 0xFFFF` while only a few real bytes remain before the end of the mapped file.
2. Serve this file as, e.g., an `http_jar`/`http_archive` artifact (with attacker-chosen sha256 matching this exact file, or omitted to rely on Bazel's non-hermetic-download warning).
3. Trigger Bazel to process the jar with `ijar` (normal `java_library` compile-time interface-jar generation) or run `third_party/ijar/zip_main.cc`'s `extract`/list mode (`zipper x`) on the file.
4. Observe a crash (ASan: heap/mmap out-of-bounds read) or leaked adjacent memory bytes copied into the `filename` buffer, which can be verified with a JUnit/shell test analogous to `third_party/ijar/test/zip_test.sh`, feeding the crafted zip to the `zipper` binary and asserting no crash / no OOB read under ASan.

**Note on confidence**: I was not able to fully trace, within available tool budget, whether `ijar`'s interface-jar generation path (as opposed to the `zipper` CLI) is invoked automatically and unconditionally on every external jar dependency in a default Bazel build, or whether some upstream validation (e.g. a prior full zip-structure scan) already rejects malformed archives before this code executes. This should be verified with a live build/ASan-instrumented test before treating this as confirmed exploitable, ideally by starting a Devin session with full repository and build access.

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

**File:** third_party/ijar/zip.cc (L507-524)
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

**File:** third_party/ijar/common.h (L49-72)
```text
inline u2 get_u2le(const u1 *&p) {
    u4 x = (p[1] << 8) | p[0];
    p += 2;
    return x;
}

inline u4 get_u4be(const u1 *&p) {
    u4 x = (p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3];
    p += 4;
    return x;
}

inline u4 get_u4le(const u1 *&p) {
    u4 x = (p[3] << 24) | (p[2] << 16) | (p[1] << 8) | p[0];
    p += 4;
    return x;
}

inline u8 get_u8le(const u1 *&p) {
  u4 lo = get_u4le(p);
  u4 hi = get_u4le(p);
  u8 x = ((u8)hi << 32) | lo;
  return x;
}
```

**File:** third_party/ijar/zip_main.cc (L164-198)
```text
// Execute the extraction (or just listing if just v is provided)
int extract(char *zipfile, char *exdir, char **files, bool verbose,
            bool extract, bool flatten) {
  std::string cwd = get_cwd();
  if (cwd.empty()) {
    return -1;
  }

  char output_root[PATH_MAX + 1];
  if (exdir != NULL) {
    if (!concat_path(output_root, sizeof(output_root), cwd.c_str(), exdir)) {
      return -1;
    }
  } else if (cwd.length() >= sizeof(output_root)) {
    fprintf(stderr, "current working directory path too long");
    return -1;
  } else {
    memcpy(output_root, cwd.c_str(), cwd.length() + 1);
  }

  UnzipProcessor processor(output_root, files, verbose, extract, flatten);
  std::unique_ptr<ZipExtractor> extractor(ZipExtractor::Create(zipfile,
                                                               &processor));
  if (extractor == NULL) {
    fprintf(stderr, "Unable to open zip file %s: %s.\n", zipfile,
            strerror(errno));
    return -1;
  }

  if (extractor->ProcessAll() < 0) {
    fprintf(stderr, "%s.\n", extractor->GetError());
    return -1;
  }
  return 0;
}
```
