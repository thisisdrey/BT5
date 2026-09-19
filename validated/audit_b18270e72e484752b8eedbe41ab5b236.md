### Title
Heap Over-Read / Memory Disclosure in `InputZipFile::ProcessCentralDirEntry` extra-field parsing - (File: `third_party/ijar/zip.cc`)

### Summary
`ijar`'s ZIP central-directory-entry parser walks a ZIP "extra field" chain using an attacker-controlled `data_size` without validating that each field stays within the extra-field region, mirroring the class of bug in `ajp_parse_data()` (parsing a length-prefixed field without bounds-checking the length against the buffer that contains it).

### Finding Description
`InputZipFile::ProcessCentralDirEntry` reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly from an attacker-supplied central directory header and then iterates the extra-field chain: [1](#0-0) 

```
p += 16;
*compressed_size = get_u4le(p);
...
const u1 *extra_p = p;
p += extra_field_length;
while (extra_p != p) {
  const u2 header_id = get_u2le(extra_p);
  const u2 data_size = get_u2le(extra_p);
  const u1 *extra = extra_p;
  extra_p += data_size;
  if (header_id == ZIP64_EXTRA_FIELD_TAG) {
    if (*uncompressed_size == U4_MAX) *uncompressed_size = get_u8le(extra);
    ...
  }
}
``` [2](#0-1) 

There is no check that `extra_p + 4 <= p` before reading `header_id`/`data_size`, nor that `extra_p + data_size <= p` before advancing `extra_p`, nor that a claimed Zip64 field's `data_size` is at least 8 bytes before `get_u8le(extra)` reads 8 bytes from it. A crafted extra field whose `data_size` does not land exactly on `p` (the end of the declared extra-field region, itself only bounded by the 16-bit `extra_field_length` taken verbatim from attacker data) causes `extra_p` to overshoot `p`. Because the loop condition is `extra_p != p`, an overshoot never re-triggers termination, so the loop continues reading 4-plus-`data_size` bytes at a time past the intended region — into subsequent central directory entries, the EOCD record, or, once past the mapped input file, unmapped/adjacent heap memory returned by `mmap`. This is the same bug class as `ajp_parse_data()`: a length field taken from untrusted input is used to advance a cursor and drive subsequent reads without verifying the length stays inside the buffer that is supposed to bound it.

Compare this to the (correctly bounded) `ExtraField::find` helper used elsewhere in the singlejar tool, which explicitly checks `byte_ptr(start) + extra_field->size() > byte_ptr(end)` before dereferencing: [3](#0-2) 
and which `output_jar.cc` additionally guards with `diag_errx` on malformed extra fields: [4](#0-3) 
`ijar/zip.cc`'s `ProcessCentralDirEntry` has no equivalent bound and predates/duplicates this logic without the fix.

`ProcessNext` further uses the (potentially memory-disclosed / corrupted) `*offset`, `*compressed_size`, and `*uncompressed_size` values to reposition the read cursor and size subsequent copies: [5](#0-4) 

### Impact Explanation
If the over-read bytes happen to be interpreted as a `ZIP64_EXTRA_FIELD_TAG` (`0x0001`) header during the runaway scan, `get_u8le(extra)` copies 8 bytes of out-of-bounds heap/adjacent-mapping memory directly into `*uncompressed_size`/`*compressed_size`/`*offset`. These values subsequently drive pointer arithmetic (`p = zipdata_in_ + in_offset_ + offset`) and downstream size-dependent copies/allocations in `ProcessFile`/`UncompressFile`, meaning process memory contents can leak into the derived interface jar (`ijar` output) that becomes a build artifact, or the wild pointer arithmetic can crash the process. This is a genuine memory-disclosure/heap-over-read primitive triggered purely by the bytes of an attacker-supplied ZIP/JAR file — no cooperation from the build author is required beyond that file being processed by `ijar`.

### Likelihood Explanation
`ijar` is a general-purpose ZIP/JAR-central-directory parser bundled with Bazel; it is exercised on any jar it strips down to an interface jar. Any workflow where the jar being stripped originates from attacker-influenced bytes (e.g., a `.jar` fetched from a mirror/registry without an enforced/pinned integrity hash, or committed on an untrusted branch that CI builds) reaches this code with fully attacker-controlled bytes. No credentials, sandbox escape, or privileged access are needed — only the ability to supply the byte content of a `.jar`/`.zip` file that Bazel's `ijar` tool later parses. This is a directly reachable, unauthenticated parsing bug in a bounds-check that other Bazel ZIP parsers (`singlejar`'s `ExtraField::find`) already implement correctly, showing the check is feasible and simply missing here.

### Recommendation
In `InputZipFile::ProcessCentralDirEntry`, before entering/continuing the extra-field loop:
1. Validate `extra_p + 4 <= p` before reading `header_id`/`data_size`.
2. Validate `extra_p + data_size <= p` before advancing `extra_p` or dereferencing `extra`.
3. For the Zip64 tag, validate `data_size >= 8*n` for however many 8-byte fields are consumed before calling `get_u8le`.
4. Change the loop termination condition from `extra_p != p` to `extra_p < p` (with the above per-iteration bound checks) so an overshoot cannot cause unbounded reads.
5. Additionally bound `extra_field_length`/`file_name_length`/`file_comment_length` against the remaining bytes in the mapped central directory before advancing `p` past them, mirroring `EnsureRemaining` already used in `ProcessLocalFileEntry`.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` style reproduction:
1. Construct a minimal ZIP/JAR whose Central Directory Header declares `extra_field_length = N` where the embedded extra-field records are crafted so the first field's `data_size` does not sum to `N` (e.g., a single extra field with `header_id` arbitrary and `data_size` larger than `N - 4`, or exactly `N-3` so `extra_p` skips one byte past `p`).
2. Place this crafted CDH as the last (or only) entry in the central directory, immediately followed by the EOCD record, and mmap the file at a page boundary so bytes past the file end (or past the CDH’s declared bounds) belong to a different heap allocation/mapping under ASan.
3. Run `ijar` (`third_party/ijar/ijar.cc` main) on this file, or drive `InputZipFile::ProcessNext()` directly in a unit test.
4. Under AddressSanitizer, `get_u2le(extra_p)`/`get_u8le(extra)` in the runaway loop will trigger a heap-buffer-overflow (read) report once `extra_p` walks past the mapped extra-field/CD region — demonstrating the over-read; without ASan, craft the overshoot to land on attacker-predictable adjacent bytes matching `ZIP64_EXTRA_FIELD_TAG` to show `*uncompressed_size`/`*offset` get set to out-of-bounds memory content, which is then observable in the resulting interface jar's local/central header size or offset fields.

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

**File:** src/tools/singlejar/zip_headers.h (L99-114)
```text
  static const ExtraField* find(uint16_t tag, const uint8_t* start,
                                const uint8_t* end) {
    while (start < end) {
      if (ziph::byte_ptr(start) + sizeof(ExtraField) > ziph::byte_ptr(end)) {
        break;
      }
      auto extra_field = reinterpret_cast<const ExtraField*>(start);
      if (ziph::byte_ptr(start) + extra_field->size() > ziph::byte_ptr(end)) {
        break;
      }
      if (extra_field->is(tag)) {
        return extra_field;
      }
      start = ziph::byte_ptr(start) + extra_field->size();
    }
    return nullptr;
```

**File:** src/tools/singlejar/output_jar.cc (L780-785)
```text
  for (const ExtraField* ef = lh_ef_begin; ef < lh_ef_end; ef = ef->next()) {
    if (ziph::byte_ptr(ef) + sizeof(ExtraField) > ziph::byte_ptr(lh_ef_end) ||
        ziph::byte_ptr(ef) + ef->size() > ziph::byte_ptr(lh_ef_end)) {
      diag_errx(1, "malformed extra field in LH for %.*s",
                (int)entry->file_name_length(), entry->file_name());
    }
```
