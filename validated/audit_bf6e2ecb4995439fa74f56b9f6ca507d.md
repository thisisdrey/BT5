### Title
Out-of-bounds read in ijar's ZIP central directory extra-field parser due to unvalidated size field - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in ijar (the tool Bazel automatically runs on every Java jar, including externally-fetched third-party jars via `http_jar`/`http_archive`/`java_import`, to build interface jars for header compilation) parses the ZIP Central Directory Header (CDH) and its extra-field records without any bounds checking, unlike its sibling function `InputZipFile::ProcessLocalFileEntry`, which calls `EnsureRemaining()` before every variable-length read.

### Finding Description
`ProcessCentralDirEntry` reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly from attacker-controlled bytes and then walks the extra-field area with: [1](#0-0) 

```cpp
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
`data_size` for each nested extra-field record is attacker-controlled and is never checked against the remaining bytes in the `[extra_p, p)` window (unlike the singlejar `ExtraField::find`, which explicitly validates `ziph::byte_ptr(start) + extra_field->size() > end` before dereferencing, see `src/tools/singlejar/zip_headers.h:97-115`). If `data_size` is crafted so `extra_p` overshoots `p`, the loop condition `extra_p != p` never becomes true again, and the loop continues reading `get_u2le`/`get_u8le` past the intended extra-field region — and past the boundaries the code itself computed from `extra_field_length` — walking arbitrarily far into the memory-mapped input file (and potentially beyond it, since there is no comparison against `input_file_->Length()` in this function, unlike `EnsureRemaining` used in `ProcessLocalFileEntry`).

This is structurally analogous to the ksmbd bug: a fixed header validates a length field (`extra_field_length`, itself unchecked here) but the code trusts a nested, attacker-supplied sub-length (`data_size`) to advance a cursor and extract/copy 64-bit ZIP64 fields (`get_u8le(extra)`) without confirming the sub-field is fully contained in the outer field/buffer.

### Impact Explanation
An attacker who publishes a jar (consumed via `http_jar`, `http_archive`, or as a `java_import`/Maven dependency) with a malformed CDH extra field triggers `ijar` to read out of bounds while producing the interface jar used for compilation. Depending on the crafted `data_size`, this can crash the build (denial of service) or, more significantly, cause `get_u8le`/`get_u4le` reads of adjacent heap/mmap memory whose bytes are then copied into `*uncompressed_size`/`*compressed_size`/`*offset`, which downstream drive subsequent pointer arithmetic (`p = zipdata_in_ + in_offset_ + offset` in `ProcessNext`). This can be leveraged for further out-of-bounds reads/writes during archive reconstruction, i.e., an integrity/memory-safety violation rooted in unvalidated variable-length metadata from untrusted content, matching CVSS AV:N/AC:L profile of the analog (though remote triggering here is via untrusted archive content, not network protocol directly).

### Likelihood Explanation
This code path executes automatically for every jar Bazel touches as part of Java compilation (ijar is invoked unconditionally to strip non-ABI members), including jars fetched from third-party/untrusted origins whose sha256 is attacker-controlled at the time of first vendoring or when consumed from an untrusted branch/CI. No special build configuration is required; default flags trigger ijar processing.

### Recommendation
Apply the same containment check ijar already omits here but uses elsewhere (`EnsureRemaining`, and singlejar's `ExtraField::find` bound checks): before dereferencing each extra-field record, verify `data_size` (and the 4-byte header itself) fits within `[extra_p, p)`, and verify `p` itself (`file_name_length + extra_field_length + file_comment_length`) does not exceed the remaining bytes of the mapped central directory / input file, mirroring `EnsureRemaining()`'s semantics used in `ProcessLocalFileEntry`.

### Proof of Concept
Construct a ZIP/JAR whose Central Directory Header advertises a small `extra_field_length` (e.g., 4 bytes) but whose single extra-field record declares `data_size = 0xFFFF`, causing `extra_p` to jump far past `p`; feed this jar through `ijar` (e.g., via a `java_import`/`http_jar` target) and observe an out-of-bounds read/crash in `InputZipFile::ProcessCentralDirEntry`, analogous to `src/tools/singlejar/output_jar_simple_test.cc`'s `CreateZipWithMalformedExtraField`/`MalformedExtraField` test (`src/tools/singlejar/output_jar_simple_test.cc:1179-1236`), but targeting `third_party/ijar/zip.cc`, which lacks the equivalent bound check that singlejar performs. [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** src/tools/singlejar/zip_headers.h (L97-115)
```text
class ExtraField {
 public:
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
