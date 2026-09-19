### Title
Integer overflow in ZIP central-directory bounds check allows out-of-bounds parsing of attacker-supplied archives - (File: third_party/ijar/zip.cc)

### Summary
`FindZipCentralDirectory` in `third_party/ijar/zip.cc` validates that the central directory described by an untrusted ZIP/JAR file lies inside the mapped input buffer using an unchecked addition: `cd.central_dir_offset + cd.central_dir_size > in_length`. Both `central_dir_offset` and `central_dir_size` are attacker-controlled values parsed directly from the End-Of-Central-Directory (EOCD) record of a downloaded archive (e.g., via `http_archive`/`http_jar`). If the two values are chosen so that their sum overflows before being compared against `in_length`, the bounds check can be bypassed, similar in spirit to the EIP-2929 gas overflow bug where an unchecked addition let an attacker-influenced value wrap around and defeat a limit check. [1](#0-0) 

### Finding Description
`FindZipCentralDirectory` reads the EOCD fields directly from attacker-supplied bytes (`cd.central_dir_size = get_u4le(current); cd.central_dir_offset = get_u4le(current);`), optionally overwritten by 64-bit ZIP64 EOCD fields via `FindZip64CentralDirectory`, and then performs:

```
if (cd.central_dir_offset + cd.central_dir_size > in_length) { ... return false; }
``` [2](#0-1) 

before computing `*central_dir = end_of_central_dir - cd.central_dir_size;` and returning it for further parsing by `ProcessCentralDirEntry`. [3](#0-2) 

In the 32-bit (non-ZIP64) path, `central_dir_offset` and `central_dir_size` are `u4` (32-bit) values read with `get_u4le`. Adding two `uint32_t` values in C++ does not implicitly widen to 64-bit before the addition; the sum wraps modulo 2^32 first, and only the (already-wrapped) result is then compared (after implicit promotion) against the 64-bit `in_length`. This means an attacker who controls both fields inside a downloaded ZIP/JAR (e.g., via `http_archive`, `http_jar`, `download_and_extract`) can pick `central_dir_offset` and `central_dir_size` such that their 32-bit sum wraps to a small value that satisfies `<= in_length`, even though the "real" (non-wrapped) offset+size would exceed the file. This is directly analogous to the reported class of bug: an unchecked/overflowable arithmetic operation on adversary-influenced operands defeats a safety/limit check (EIP-2929's gas accumulation check vs. Bazel's archive-bounds check).

Once the check is bypassed, `end_of_central_dir - cd.central_dir_size` can produce a pointer that is not actually within the mapped buffer (pointer arithmetic with a bogus `central_dir_size`), and the subsequent central-directory entry parsing (`ProcessCentralDirEntry`, which walks the buffer using offsets/lengths taken from the (now unreliable) central directory pointer) can read outside of the mapped file region under `MappedInputFile`. `EnsureRemaining`-style bounds checks exist elsewhere in local-file-entry parsing but the central-directory pointer itself is derived from the overflowed arithmetic and is not re-validated against the actual mapped range at this point. [4](#0-3) 

This code path is reached whenever Bazel processes a JAR/ZIP produced by an external, potentially hostile source — e.g. archives fetched by `http_archive`/`http_jar` rules where the attacker controls the served bytes and only a `sha256`/`integrity` value pins content identity, not the internal structural correctness of the ZIP format itself. The checksum check happens at download time and gates whether the *bytes* match a pinned digest, but does not protect against a maliciously crafted archive whose bytes genuinely match a benign-looking hash chosen by the attacker who is the origin server for a new/first-time dependency (no pre-existing pin), or any consumer of ijar-processed jars where content is not required to be pinned (e.g. jar merging/stripping tools operating on build outputs or extracted archives).

### Impact Explanation
If reachable with attacker-controlled offset/size values that wrap the 32-bit addition, this could cause the ZIP/JAR parser to compute a central-directory pointer or entry bounds outside the actual mapped file, leading to out-of-bounds reads (and potentially corrupting downstream metadta parsing, crashes, or leaking adjacent memory content into parsed strings/filenames) when Bazel or ijar processes a hostile archive. This does not by itself provide arbitrary write, credential exfiltration, or lockfile/checksum bypass of a *pinned* digest — the checksum still binds the byte content — but it is a genuine "unchecked-overflow defeats a bounds/limit check" flaw in code that processes externally-supplied byte streams, matching the reported bug class.

### Likelihood Explanation
I could not fully confirm without running code whether `u4` is exactly `uint32_t` (I was unable to complete verification of `third_party/ijar/common.h` in the final iteration), nor could I confirm whether `ProcessCentralDirEntry`/downstream consumers re-validate the computed `central_dir` pointer against the mapped buffer boundaries elsewhere (there may be an additional guard I did not locate). Because of this residual uncertainty, and because triggering a genuinely exploitable OOB read (rather than just an early rejection) requires a valid-looking EOCD with wrapped values that still pass all the other structural cross-checks in `MaybeReadZip64CentralDirectory`, the practical exploitability is uncertain without a concrete reproduction.

### Recommendation
Perform the bounds arithmetic in a 64-bit (or wider, overflow-checked) domain consistently, e.g. cast both operands to `uint64_t`/`size_t` before adding, and explicitly check for overflow (`offset > in_length || size > in_length - offset`) rather than relying on the raw sum, mirroring the upstream go-ethereum fix's pattern of checking for overflow before comparing against a limit. Apply the same overflow-safe pattern to any other 32-bit offset/size additions in `third_party/ijar/zip.cc` and `src/tools/singlejar/*` that combine attacker-controlled ZIP header fields.

### Proof of Concept
I do not have a validated, reproducible JUnit/shell PoC for this finding — I was unable to fully verify the `u4`/`u8` type widths and confirm the absence of an additional bounds re-check downstream within the remaining tool budget. A concrete PoC would need to:
1. Craft a ZIP file whose 32-bit `central_dir_offset` and `central_dir_size` fields sum to a value that wraps `uint32_t` (e.g., `offset = 0xFFFFFFF0`, `size = 0x20`, sum wraps to `0x10`), while remaining structurally acceptable to `MaybeReadZip64CentralDirectory`'s cross-checks (or omitting a ZIP64 record entirely).
2. Feed this archive through `bazel build` via `http_archive`/`http_jar` or directly through the `ijar`/`singlejar` tools, and observe whether `FindZipCentralDirectory` accepts it and produces an out-of-bounds `central_dir` pointer, verified via ASan/valgrind.

Given the inability to confirm this end-to-end in the available time, this should be treated as a **candidate** finding requiring further verification (exact type widths, and confirmation that no additional bounds check exists) before being considered conclusively exploitable.

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

**File:** third_party/ijar/zip.cc (L744-778)
```text
  EndOfCentralDirectoryRecord cd;
  const u1* end_of_central_dir = current;
  get_u4le(current);  // central directory locator signature, already checked
  cd.number_of_this_disk = get_u2le(current);
  cd.disk_with_central_dir = get_u2le(current);
  cd.central_dir_entries_on_this_disk = get_u2le(current);
  cd.central_dir_entries = get_u2le(current);
  cd.central_dir_size = get_u4le(current);
  cd.central_dir_offset = get_u4le(current);
  u2 file_comment_length = get_u2le(current);
  current += file_comment_length;  // set current to the end of the central dir

  if (!FindZip64CentralDirectory(bytes, in_length, &end_of_central_dir, &cd)) {
    return false;
  }

  if (cd.number_of_this_disk != 0 || cd.disk_with_central_dir != 0 ||
      cd.central_dir_entries_on_this_disk != cd.central_dir_entries) {
    fprintf(stderr, "multi-disk JAR files are not supported\n");
    return false;
  }

  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }

  // Do not change output values before determining that they are OK.
  *offset = cd.central_dir_offset;
  // Central directory start can then be used to determine the actual
  // starts of the zip file (which can be different in case of a non-zip
  // header like for auto-extractable binaries).
  *central_dir = end_of_central_dir - cd.central_dir_size;
  return true;
}
```
