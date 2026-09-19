### Title
Out-of-bounds heap read parsing ZIP64 extra fields in `InputZipFile::ProcessCentralDirEntry` - (File: `third_party/ijar/zip.cc`)

### Summary
`third_party/ijar/zip.cc` is ijar/singlejar's ZIP/JAR reader, used by Bazel to unpack interface jars and other archive inputs whose bytes originate from an untrusted, attacker-published source (e.g., a downloaded `.jar`/archive dependency). `InputZipFile::ProcessCentralDirEntry` parses the ZIP central directory extra-field records with no bounds validation on the declared `data_size`, mirroring the class of bug in `php_jpg_get16` (CVE-2019-11040): a fixed-width field is read out of a buffer using an attacker-controlled length/offset without checking that enough bytes actually remain.

### Finding Description
In `ProcessCentralDirEntry` [1](#0-0) , after the central directory header's fixed fields are parsed, the code walks the "extra fields" region:

```
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
```

Two separate but related bugs exist here, both directly attacker-controllable via the bytes of an archive:

1. `get_u8le(extra)` unconditionally reads 8 bytes from `extra` for each of `uncompressed_size`, `compressed_size`, and `offset` whenever the Zip64 tag is seen, but `data_size` — the length of that specific extra-field record — is never checked to be `>= 8`, `>= 16`, or `>= 24` depending on how many of the three fields are actually present (per APPNOTE 4.5.3, the Zip64 extra field's payload is variable-length and only contains the subset of 8-byte fields whose 32-bit counterpart is `0xFFFFFFFF`). A malicious archive can set `data_size` to e.g. `1` or `4` while still tripping the `0xFFFFFFFF` sentinel checks for one or more of `uncompressed_size`/`compressed_size`/`offset`, causing `get_u8le` to read up to 24 bytes starting at `extra` — past the declared/actual extra-field payload and potentially past the mapped file entirely.
2. The loop terminates on `extra_p != p` (pointer equality with the extra-fields end), but nothing prevents `extra_p` from overshooting `p` if a crafted `data_size` doesn't line up with the declared `extra_field_length` — this can drive `extra_p` beyond the buffer and keep looping/reading heap memory beyond the mapped input file.

Unlike `InputZipFile::ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before consuming `file_name`/`extra_field` bytes [2](#0-1) , `ProcessCentralDirEntry` performs no equivalent remaining-bytes check before or during the extra-field walk [3](#0-2) . The comment at the top of the function ("the central directory is always followed by another data structure that has a signature, so parsing it this way is safe") only justifies the *outer* central-directory-entry walk, not the inner extra-field loop's unchecked 8-byte reads.

For comparison, singlejar's own analogous ZIP header parser (`src/tools/singlejar/zip_headers.h`, `Zip64ExtraField::find`) is bounds-checked, and its own test suite exercises well-formed Zip64/UT extra fields [4](#0-3) , but ijar's `zip.cc` implementation (used for producing/consuming interface jars) does not share this hardening.

### Impact Explanation
A crafted `.jar`/`.zip` served from a malicious or compromised dependency URL that Bazel fetches, or an attacker-controlled artifact placed in an untrusted-branch build, can cause `ijar`/singlejar's ZIP reader to read up to tens of bytes past the mapped input buffer. This is an out-of-bounds heap/mmap read: at minimum it can crash the build tool (denial of service local to processing that archive), and depending on memory layout it can leak adjacent heap/mmap contents into computed size/offset values that influence subsequent parsing (e.g., used as `*compressed_size`/`*offset` to seek into the file), which is the same "read past allocated buffer" primitive as the CVE-2019-11040 analog (`php_jpg_get16`). No checksum/integrity mechanism in Bazel's download/extraction path (the `EnsureRemaining` check exists only in the sibling local-file-header parser) covers this specific internal parsing path.

### Likelihood Explanation
Moderate-to-high: any external report/dependency archive Bazel unpacks with `ijar` (or any code path that reuses `third_party/ijar/zip.cc`'s `InputZipFile`) is untrusted content from the attacker's perspective (a malicious ZIP entry crafted with a Zip64 extra field whose `data_size` is smaller than what the `0xFFFFFFFF` sentinel fields imply). No special privileges are needed — only the ability to have Bazel process a hostile ZIP/JAR file, which matches the "unprivileged, content published/served" attacker model.

### Recommendation
In `ProcessCentralDirEntry`, before reading each Zip64 sub-field:
- Validate `data_size` against the number of 8-byte fields actually needed (based on which of `uncompressed_size`/`compressed_size`/`offset` equal `U4_MAX`), rejecting/erroring if `data_size` is insufficient.
- Bound the `while (extra_p != p)` loop using `extra_p < p` combined with a check that each `header_id`/`data_size` read and subsequent `extra_p += data_size` never exceeds `p`/the mapped file end, mirroring the `EnsureRemaining()` pattern already used in `ProcessLocalFileEntry`.
- Reuse or align with the already-hardened `Zip64ExtraField::find` bounds-checked implementation in `src/tools/singlejar/zip_headers.h` rather than maintaining a second, unchecked parser in `third_party/ijar/zip.cc`.

### Proof of Concept
A `BuildIntegrationTestCase`/unit test analogous to `zip_headers_test.cc`'s `LocalHeader`/`CentralDirectoryHeader` tests, but targeting `InputZipFile::ProcessCentralDirEntry` (or an end-to-end `ijar`/singlejar invocation) with a crafted ZIP central directory entry:
1. Set `compressed_file_size32`/`uncompressed_file_size32`/`local_header_offset32` to `0xFFFFFFFF` (to trigger all three Zip64 sentinel checks).
2. Attach a Zip64 extra field (`header_id = 0x0001`) with `data_size` set to `4` (or `0`) — far smaller than the `24` bytes required for all three sentinel fields.
3. Place this crafted central directory record at the very end of a small mmapped buffer (or run under ASan) so that reading 8/16/24 bytes for `get_u8le` calls walks past the allocated/mapped region.
4. Run the parser (via `InputZipFile::ProcessNext`) under AddressSanitizer and confirm a heap-buffer-overflow (read) is reported inside `ProcessCentralDirEntry`.

I was unable to fully trace `InputZipFile::Open()`/`LocateCentralDirectory()` (the code that determines the buffer boundaries and how the central directory pointer `p` relates to the mmap'd file end) due to indexing limits on `third_party/ijar/zip.cc`; a Devin session with full file access would be needed to confirm the exact allocation size guarantees and finalize a runnable ASan-based PoC.

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

**File:** src/tools/singlejar/zip_headers_test.cc (L108-122)
```text
    const Zip64ExtraField* zip64_field = lh->zip64_extra_field();
    ASSERT_NE(nullptr, zip64_field);
    EXPECT_EQ(16, zip64_field->payload_size());
    EXPECT_EQ(20, zip64_field->size());
    EXPECT_EQ(5000000000UL, zip64_field->attr64(0));
    EXPECT_EQ(3000000UL, zip64_field->attr64(1));

    const UnixTimeExtraField* ut_extra_field = lh->unix_time_extra_field();
    ASSERT_NE(nullptr, ut_extra_field);
    EXPECT_EQ(9, ut_extra_field->payload_size());
    EXPECT_EQ(13, ut_extra_field->size());
    EXPECT_EQ(2, ut_extra_field->timestamp_count());
    EXPECT_TRUE(ut_extra_field->has_modification_time());
    EXPECT_TRUE(ut_extra_field->has_access_time());
    EXPECT_FALSE(ut_extra_field->has_creation_time());
```
