## Title
Out-of-bounds heap read parsing ZIP central directory entries in ijar's `InputZipFile::ProcessCentralDirEntry` - (File: `third_party/ijar/zip.cc`)

## Summary
`FindZipCentralDirectory` validates that `cd.central_dir_offset + cd.central_dir_size <= in_length` before returning the central directory pointer [1](#0-0) , but the per-entry parser `InputZipFile::ProcessCentralDirEntry` (and its caller `CalculateOutputLength`) then walks entries using only the fields it reads out of that same buffer, with no per-field bounds check against the mapped file length [2](#0-1) .

## Finding Description
`ProcessCentralDirEntry` reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly from attacker-controlled bytes and advances the cursor `p` by their sum without ever checking that `p` (or the memcpy'd region) stays inside `[bytes, bytes+in_length)`: [3](#0-2) 

The `memcpy` at line 520 copies `file_name_length` bytes from `p` into a fixed `filename[PATH_MAX]` buffer with only a truncation check against `filename_size`, not against how many bytes are actually left in the mapped input — if a hostile ZIP entry claims a `file_name_length`/`extra_field_length`/`file_comment_length` that extends past the previously-validated `central_dir_offset + central_dir_size` boundary (e.g., by lying about entry count so the loop keeps consuming beyond the last real entry, or by placing an oversized length on the last entry), `p` walks past the end of the mmap'd file. Compare this to `InputZipFile::ProcessLocalFileEntry`, which explicitly guards every field read via `EnsureRemaining()` [4](#0-3)  — the central-directory path has no equivalent guard. The comment above the function even claims "the central directory is always followed by another data structure that has a signature, so parsing it this way is safe" [5](#0-4) , which is only true if the length fields inside each CDH entry are internally consistent with `central_dir_size` — a property that is never independently verified per-entry.

This is invoked both from `InputZipFile::ProcessNext` (used by the `unzipper`/`zipper` command-line tool and by `ijar`'s interface-jar generation over untrusted jars) and from `CalculateOutputLength`, which loops over the whole central directory unconditionally [6](#0-5) .

## Impact Explanation
A crafted `.jar`/`.zip` (e.g., a third-party jar dependency fed to `ijar`, or a zip processed by the `zipper`/`unzipper` binaries during a build) can trigger reads past the mmap'd file boundary. This is a memory-safety bug (SIGSEGV/info leak into subsequent parsed fields) reachable purely from bytes an unprivileged party controls (a malicious jar artifact) — directly analogous to the tiffcrop OOB read pattern: attacker-supplied structured binary data with self-declared, unchecked length fields consumed by a fixed-format parser.

## Likelihood Explanation
Reachable whenever bazel/ijar processes an externally-supplied jar (common for `java_import`/`ijar` interface-jar stripping of fetched Maven/http_jar artifacts) or when the `zipper`/`unzipper` tool is run on an untrusted archive. No credentials or special privileges are required — only that the victim's build consumes an attacker-crafted zip/jar. However, per the rules, straightforward SHA256/integrity checking on the *download* step (in `http_archive`/`http_jar`) does not gate this path, since ijar reprocesses the archive's internal structure after the whole file already passed the checksum — the vulnerability is purely in the internal parser's failure to bound-check length fields relative to the already-validated `central_dir_size`, not a bypass of the download checksum itself.

## Recommendation
Add explicit bounds checks in `ProcessCentralDirEntry` (mirroring `EnsureRemaining` in `ProcessLocalFileEntry`) verifying that `p + file_name_length`, `p + extra_field_length`, and `p + file_comment_length` (and the inner extra-field walk) never exceed the previously validated central-directory end (`central_dir_ + central_dir_size` / `zipdata_in_ + input_file_->Length()`), returning an error instead of continuing to advance `p` unchecked.

## Proof of Concept
A `src/test/shell` or ijar-level `zip_test.sh`/gtest reproduction: build a ZIP whose valid `EOCD`/central-directory bounds pass `FindZipCentralDirectory`'s check, but where the last Central Directory Header entry declares `file_name_length`/`extra_field_length`/`file_comment_length` values that push the read cursor past `bytes + in_length` (while total central_dir_size still nominally matches). Running `${ZIPPER} x crafted.zip` or `ijar crafted.jar out.jar` over this file should be observed (under ASan) to read out of the mapped buffer, analogous to `test_no_path_traversal`'s existing crafted-jar regression pattern in `third_party/ijar/test/zip_test.sh` [7](#0-6) .

**Caveat**: I was unable to fully trace every mmap allocation-size guard (e.g., page-boundary slack in `MappedInputFile`) that might silently absorb small over-reads in practice; a background agent with ASan tooling should confirm the OOB read is externally observable (crash or leaked data) versus merely reading unmapped-but-still-committed pages, before finalizing severity.

### Citations

**File:** third_party/ijar/zip.cc (L332-370)
```text
int InputZipFile::ProcessLocalFileEntry(
    size_t compressed_size, size_t uncompressed_size) {
  if (EnsureRemaining(26, "extract_version") < 0) {
    return -1;
  }
  extract_version_ = get_u2le(p);
  general_purpose_bit_flag_ = get_u2le(p);

  if ((general_purpose_bit_flag_ & ~GENERAL_PURPOSE_BIT_FLAG_SUPPORTED) != 0) {
    return error("Unsupported value (0x%04x) in general purpose bit flag.\n",
                 general_purpose_bit_flag_);
  }

  compression_method_ = get_u2le(p);

  if (compression_method_ != COMPRESSION_METHOD_DEFLATED &&
      compression_method_ != COMPRESSION_METHOD_STORED) {
    return error("Unsupported compression method (%d).\n",
                 compression_method_);
  }

  // skip over: last_mod_file_time, last_mod_file_date, crc32
  p += 2 + 2 + 4;
  compressed_size_ = get_u4le(p);
  uncompressed_size_ = get_u4le(p);
  file_name_length_ = get_u2le(p);
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

**File:** third_party/ijar/zip.cc (L491-492)
```text
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
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

**File:** third_party/ijar/zip.cc (L550-581)
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

  // The worst case is when the output is simply the input uncompressed. The
  // metadata in the zip file will stay the same, so the file will grow by the
  // difference between the compressed and uncompressed sizes.
  return (u8) input_file_->Length() - skipped_compressed_size
      + (uncompressed_size - compressed_size);
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
