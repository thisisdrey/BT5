### Title
Unvalidated extra-field size in ZIP central directory parsing leads to out-of-bounds read - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in ijar's ZIP reader parses the Central Directory Header's `file_name_length`, `extra_field_length`, and, inside the extra-field records, an attacker-controlled `data_size` field, and advances the cursor / dereferences memory based on those values without ever validating them against the actual remaining bytes in the mapped input file.

### Finding Description
`InputZipFile::ProcessLocalFileEntry` explicitly guards every variable-length field read with `EnsureRemaining()` before trusting attacker-supplied lengths: [1](#0-0) 

By contrast, `ProcessCentralDirEntry`, which walks the Central Directory (an area whose overall size is taken from the End-Of-Central-Directory record but never re-validated per field), reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly off the mmap'd buffer and then advances `p` by those raw values with no equivalent bounds check: [2](#0-1) 

Worse, inside the same function, the extra-field sub-record parsing loop reads a 2-byte `header_id` and a 2-byte `data_size` (fully attacker-controlled, up to 65535) and blindly advances `extra_p += data_size`, then — if the tag matches `ZIP64_EXTRA_FIELD_TAG` — calls `get_u8le(extra)` up to three times to read 8-byte values from `extra`, without ever checking that `data_size >= 8` or that `extra_p + data_size` (or `extra + 8`) stays within the extra-field buffer or the mapped file at all: [3](#0-2) 

This is the same bug class as CVE-2016-20022: a length/size field taken from untrusted, attacker-supplied input (there, `wMaxPacketSize` in a USB descriptor; here, `data_size`/`extra_field_length` in a ZIP Central Directory Header) is used to index/advance into a buffer without validating it against the buffer's actual bounds. In contrast, this repository's own `ExtraField::find()` helper (used elsewhere for Local Header extra fields) *does* perform this bounds check: [4](#0-3) 

which shows the invariant ("extra-field size must not exceed the enclosing buffer") is understood and enforced in one ZIP parser (`src/tools/singlejar`) but not in the other (`third_party/ijar`), and specifically not in the Central Directory extra-field loop of `ProcessCentralDirEntry`.

### Impact Explanation
`ijar` (via `zip.cc`) is invoked by Bazel automatically to strip non-public interfaces out of every jar used as a Java compile-time dependency (`ijar`/`interface jar` generation), and such jars are routinely supplied by external, attacker-influenceable sources (e.g., `http_jar`, `http_archive`-fetched Maven artifacts, or `maven_install`-resolved jars pinned only by a `sha256`). Because the checksum only certifies that the bytes match a previously recorded digest — not that the ZIP's internal structure is well-formed — an attacker who is able to get a maliciously crafted, but hash-matching, jar accepted as a dependency (e.g. by publishing the malicious jar first and letting a victim pin its hash, or via any scenario where the pinned digest was computed over already-malicious bytes) can trigger out-of-bounds reads past the mapped input file when Bazel processes that jar with `ijar`. This can crash the build (denial of service) or, depending on what lies past the mapping, leak adjacent memory contents into the derived interface jar / error output.

### Likelihood Explanation
`ijar` runs unconditionally as part of ordinary `java_library`/`java_import` compilation actions whenever interface jars are needed, so the code path is reached on virtually every Bazel Java build without any special configuration. The malformed-length values (`extra_field_length`, `data_size`) are two-byte fields fully controlled by whoever crafts the ZIP/JAR bytes, requiring no special privileges — only the ability to have Bazel consume the crafted jar as a build input.

### Recommendation
Add the same bounds validation used by `ExtraField::find()` (and by `ProcessLocalFileEntry`'s `EnsureRemaining()`) to `ProcessCentralDirEntry`: before advancing `p` by `file_name_length`/`extra_field_length`/`file_comment_length`, verify the values do not run past the mapped file end; and inside the extra-field iteration loop, verify `data_size` does not exceed the remaining bytes in the extra-field block before advancing `extra_p`, and verify at least 8 bytes remain before each `get_u8le()` call.

### Proof of Concept
A concrete JUnit/`src/test/shell/bazel` reproduction could not be fully constructed within this investigation; a definitive PoC would need to be built and validated with actual `ijar` binary execution (mmap-based OOB reads may only manifest as a crash under ASan or with a file mapped at a page boundary) — this is best done as: construct a minimal ZIP/JAR whose Central Directory Header entry declares `extra_field_length` (or an inner `data_size`) larger than the bytes actually present before the End-Of-Central-Directory record, run it through the `ijar` binary (or `singlejar`, to compare against the already-hardened `ExtraField::find` path), and observe (ideally under AddressSanitizer) an out-of-bounds read/crash in `ProcessCentralDirEntry`. I was not able to verify at the file-mapping layer (`MappedInputFile`) whether any implicit guard page or size check exists that might already mitigate this in practice; this should be confirmed by a background agent with build/test execution access before treating this as fully validated.

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

**File:** third_party/ijar/zip.cc (L507-542)
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
