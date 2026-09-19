### Title
Out-of-bounds read decoding Zip64 extended-information sub-fields with a too-small declared `payload_size` - (File: `src/tools/singlejar/zip_headers.h`)

### Summary
`Zip64ExtraField::attr64(int index)` indexes a variable-length `attr_[]` array using an index chosen by the *caller* (`CDH`/`LH`) based on which 32-bit size/offset fields equal `0xFFFFFFFF`, but never checks that index against `attr_count()` (derived from the extra field's own declared `payload_size`). This mirrors the busybox CVE-2019-5747 bug class: the code validates only that *some* Zip64 extra-field container is present, not that it is long enough to hold the specific fixed-size sub-value being decoded.

### Finding Description
`ExtraField::find()` (`src/tools/singlejar/zip_headers.h:99-115`) only bounds-checks that a candidate extra field's own declared `size()` (4-byte header + `payload_size()`) fits inside `[start, end)` of the enclosing `extra_fields` blob: [1](#0-0) 

`Zip64ExtraField::attr64(int index)` then reads `attr_[index]` unconditionally: [2](#0-1) 

`CDH`/`LH` decide *how many* 64-bit attributes must be present, purely from the central/local header's own 32-bit fields being `0xFFFFFFFF` (a sentinel meaning "value is in the Zip64 extra field"): [3](#0-2) [4](#0-3) [5](#0-4) 

Nothing cross-checks that `zip64_extra_field()->attr_count()` (i.e. its declared `payload_size() / 8`) actually covers the index being requested (0, 1, or up to 2 for `local_header_offset()`). A crafted CDH/LH entry can set e.g. `uncompressed_file_size32 = 0xFFFFFFFF` while its accompanying Zip64 extra field declares `payload_size = 0` (or fewer than the needed 8/16/24 bytes). `ExtraField::find()` will happily accept this tiny/empty field because its own declared size fits in the extra-fields buffer, but `attr64(0)` will then read 8 bytes starting immediately past the field's actual (correctly-bounds-checked) payload — i.e., past the declared field boundary into whatever memory follows (subsequent extra fields, the file comment, or, if the Zip64 field is the last bytes of an mmapped/heap-allocated buffer, out of that allocation entirely).

This is architecturally identical to the busybox bug: an "assurance that *a* length field decodes" (the outer TLV container fits) substituting for "assurance that *the specific fixed-size value* has enough bytes" — exactly the incomplete-fix pattern named in the CVE description ("assurance of a 4-byte length when decoding DHCP_SUBNET").

### Impact Explanation
`zip_headers.h` underlies `ijar`/`singlejar` (`src/tools/singlejar/output_jar.cc`, `combiners.cc`), which are invoked as ordinary Bazel build actions processing JAR files that can originate from external dependencies (e.g. `http_jar`, Maven artifacts) — content an unprivileged attacker fully controls (they choose the bytes; Bazel only pins a hash of those attacker-chosen bytes, which does not constrain their internal structure). A malformed Zip64 extra field triggers a heap out-of-bounds read whose corrupted 64-bit result is subsequently used as a size/offset for further parsing/copying (`in_zip_size()`, `local_header_offset()`), which can cascade into larger over-reads, crashes (worker/action failure), or use of adjacent heap bytes as size/offset in downstream file operations during action execution.

### Likelihood Explanation
Reaching this requires only building a syntactically valid but structurally malformed ZIP/JAR (a Zip64 extra field with an undersized `payload_size` alongside a 0xFFFFFFFF sentinel in the corresponding 32-bit CDH/LH field) — trivial to construct with any hex editor or zip library, no special privileges, and no interaction with the victim's machine beyond publishing the artifact that a build later fetches/consumes.

### Recommendation
In `Zip64ExtraField`, validate `attr_count()` against the maximum index requested before calling `attr64()` in `CDH::compressed_file_size()`, `CDH::uncompressed_file_size()`, `CDH::local_header_offset()`, and the analogous `LH` methods; treat an under-sized Zip64 extra field as "absent"/error rather than silently over-reading. Likewise bound-check `UnixTimeExtraField::timestamp_count()` against `flags_` before indexing `timestamp_[]`.

### Proof of Concept
A `zip_headers_test.cc`-style unit test can demonstrate the gap directly: construct a `CDH`/`LH` buffer whose `uncompressed_file_size32`/`compressed_file_size32`/`local_header_offset32` are set to `0xFFFFFFFF`, but whose trailing Zip64 extra field is written with `payload_size = 0` (or `8`, one attribute short of what the sentinel combination requires) placed at the very end of the allocated `extra_fields`/header buffer (e.g. sized exactly via a heap allocation with no trailing padding, ideally run under ASan). Calling `cdh->uncompressed_file_size()` / `cdh->local_header_offset()` in that configuration invokes `attr64(index)` for an `index >= attr_count()`, reading past the field's own bounds-checked region; under AddressSanitizer this manifests as a heap-buffer-overflow read, proving the OOB access that the current `ExtraField::find()` bounds check fails to prevent.

*Note: I was not able to trace this to a specific reachable call site inside `output_jar.cc`/`combiners.cc` within this session's iteration budget (only confirmed via `grep` that those files call `attr64`/`compressed_file_size()`/`uncompressed_file_size()`); a full proof-of-concept exercising the actual `singlejar`/`ijar` action pipeline end-to-end (rather than the header-parsing unit alone) would need further verification of exact offsets/allocation sizes to guarantee the read lands outside a live allocation.*

### Citations

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

**File:** src/tools/singlejar/zip_headers.h (L159-165)
```text
  // The value of i-th attribute
  uint64_t attr64(int index) const { return le64toh(attr_[index]); }
  void attr64(int index, uint64_t v) { attr_[index] = htole64(v); }

  // Attribute count
  int attr_count() const { return payload_size() / sizeof(attr_[0]); }
  void attr_count(int n) { payload_size(n * sizeof(attr_[0])); }
```

**File:** src/tools/singlejar/zip_headers.h (L406-415)
```text
  size_t compressed_file_size() const {
    size_t size32 = compressed_file_size32();
    if (ziph::zfield_has_ext64(size32)) {
      const Zip64ExtraField* z64 = zip64_extra_field();
      return z64 == nullptr ? 0xFFFFFFFF
                            : z64->attr64(ziph::zfield_has_ext64(
                                  uncompressed_file_size32()));
    }
    return size32;
  }
```

**File:** src/tools/singlejar/zip_headers.h (L423-430)
```text
  size_t uncompressed_file_size() const {
    uint32_t size32 = uncompressed_file_size32();
    if (ziph::zfield_has_ext64(size32)) {
      const Zip64ExtraField* z64 = zip64_extra_field();
      return z64 == nullptr ? 0xFFFFFFFF : z64->attr64(0);
    }
    return size32;
  }
```

**File:** src/tools/singlejar/zip_headers.h (L482-493)
```text
  uint64_t local_header_offset() const {
    uint32_t size32 = local_header_offset32();
    if (ziph::zfield_has_ext64(size32)) {
      const Zip64ExtraField* z64 = zip64_extra_field();
      int attr_no = ziph::zfield_has_ext64(uncompressed_file_size32());
      if (ziph::zfield_has_ext64(compressed_file_size32())) {
        ++attr_no;
      }
      return z64 == nullptr ? 0xFFFFFFFF : z64->attr64(attr_no);
    }
    return size32;
  }
```
