### Title
Out-of-bounds heap read parsing crafted ZIP/JAR central-directory extra fields in ijar - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessCentralDirEntry` in ijar (used by singlejar/ijar tooling to read `.jar`/`.zip` archives for interface-jar generation, deploy-jar merging, and archive extraction) parses the variable-length "extra field" region of a Central Directory Header entry without any bounds checking against the declared `extra_field_length`, unlike the analogous, properly-bounded `ExtraField::find` helper used elsewhere in the codebase's singlejar zip header parser.

### Finding Description
`ProcessCentralDirEntry` reads a Central Directory Header, then walks the "extra field" bytes between `p` (start of extra fields) and `p + extra_field_length` looking for a Zip64 extra field tag: [1](#0-0) 

```
p += file_name_length;
const u1 *extra_p = p;
p += extra_field_length;
while (extra_p != p) {
  const u2 header_id = get_u2le(extra_p);
  const u2 data_size = get_u2le(extra_p);
  const u1 *extra = extra_p;
  extra_p += data_size;
  ...
}
```

There is no check that `extra_p + 4 <= p` before reading `header_id`/`data_size`, nor that `extra_p + data_size <= p` before advancing. `data_size` is a fully attacker-controlled 16-bit value read straight from the crafted archive. If a hostile/crafted zip declares an extra-field record whose `data_size` does not evenly divide the remaining extra-field region (or exceeds it), `extra_p` will overshoot `p` instead of landing exactly on it. Because the loop condition is `extra_p != p` (not `extra_p < p`), the loop does not terminate when overshooting — it continues reading 4-byte header/size pairs from memory that lies past the declared extra-field region (potentially past the mapped file into other heap memory, or off the end of the mmap'd input), i.e., an out-of-bounds read, directly analogous to the unchecked packed-pixel read in `putcontig8bitCIELab` in the CVE.

This contrasts with the properly bounds-checked walker used by the other zip-header parser in the same codebase (singlejar's `ExtraField::find`), which explicitly checks `byte_ptr(start) + sizeof(ExtraField) > byte_ptr(end)` and `byte_ptr(start) + extra_field->size() > byte_ptr(end)` before dereferencing: [2](#0-1) 

ijar's own `ProcessLocalFileEntry`, by comparison, does call `EnsureRemaining()` before reading `file_name`/`extra_field` regions from the local header: [3](#0-2) 

but this same discipline is absent from `ProcessCentralDirEntry`'s inner extra-field loop, and `EnsureRemaining` itself only guards `p` relative to the mapped input length, not the inner `extra_p` cursor used in this loop: [4](#0-3) 

### Impact Explanation
An out-of-bounds heap read while reading the memory-mapped input archive. This can cause a crash (denial of service against the build) when it walks off the end of the mapped file, or read adjacent unrelated heap/mmap data that gets interpreted as Zip64 size/offset values and subsequently used to compute `compressed_size`/`uncompressed_size`/`offset` for extraction, which is then trusted by later archive-extraction logic (`ProcessLocalFileEntry`, `ProcessFile`). This maps to the reachable class named in the prompt's guidance (`ZipDecompressor`/extraction and parsing surface) and is directly analogous to `putcontig8bitCIELab`'s unchecked read over packed, attacker-supplied byte layout.

### Likelihood Explanation
Reachable whenever ijar or singlejar processes a `.jar`/`.zip` archive that is not from a fully trusted, hash-verified source — e.g., a jar file checked into an untrusted branch that CI subsequently builds with `java_import`/`ijar`, or an archive fetched without a verified content hash. The attacker only needs to control the bytes of a Central Directory Header's extra-field bytes to trigger the mis-parse; no privileged access is required.

### Recommendation
Add explicit bounds checks in `ProcessCentralDirEntry`'s extra-field loop mirroring `ExtraField::find` in `zip_headers.h`: before reading `header_id`/`data_size`, verify `extra_p + 4 <= p`; after computing `data_size`, verify `extra_p + data_size <= p` before advancing, and abort/error out (as `ExtraField::find` does by `break`ing) rather than continuing to read past the declared extra-field region.

### Proof of Concept
Construct a crafted zip/jar whose Central Directory Header entry declares `extra_field_length` = 4 (one header/size pair) with a `data_size` value that does not equal 0 (e.g., `data_size` = 0xFFFF), so that `extra_p` after `extra_p += data_size` lands far past `p`. Feed this file to `ijar`/`singlejar`'s `InputZipFile::ProcessNext` → `ProcessCentralDirEntry`; observe the `while (extra_p != p)` loop failing to terminate at the intended boundary and continuing to dereference `get_u2le` on memory beyond the extra-field bytes (and potentially beyond the mapped file), producing an out-of-bounds read/crash — a `BuildIntegrationTestCase`/`src/test/shell/bazel` test can drive this by having a target depend on such a crafted jar via `java_import` or `http_jar` without hash verification and asserting the build crashes or reads unexpected data instead of failing cleanly with a parse error.

### Citations

**File:** third_party/ijar/zip.cc (L161-170)
```text
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

**File:** third_party/ijar/zip.cc (L523-542)
```text
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
```

**File:** src/tools/singlejar/zip_headers.h (L99-115)
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
  }
```
