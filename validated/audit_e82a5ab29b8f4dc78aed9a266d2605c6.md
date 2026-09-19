### Title
Out-of-bounds read in `Zip64ExtraField::attr64` from unvalidated attribute-count vs. 32-bit overflow flags - ([File: src/tools/singlejar/zip_headers.h])

### Summary
Bazel's `singlejar` tool (used by `java_binary`/`java_import` deploy-jar construction and other rules that merge zip/jar inputs) parses `LH`/`CDH` records of attacker-supplied `--sources` jars. When a 32-bit size/offset field equals `0xFFFFFFFF`, the code unconditionally reads the corresponding 64-bit value out of the record's `Zip64ExtraField` via `attr64(index)`, without ever checking that the field's declared `attr_count()` actually contains that many 64-bit slots.

### Finding Description
`ExtraField::find()` in [1](#0-0)  only validates that a candidate extra field, based on its own declared `payload_size()`, fits within the enclosing `extra_fields` buffer (bounded by `extra_fields_length()`). It never checks the relationship between the *number* of Zip64 attributes actually present (`attr_count() == payload_size()/8`) and which of the 32-bit `compressed_file_size32()`/`uncompressed_file_size32()`/`local_header_offset32()` fields are marked `0xFFFFFFFF` (i.e., "value lives in Zip64 extra field").

`LH::compressed_file_size()` [2](#0-1)  and `LH::uncompressed_file_size()` [3](#0-2)  each independently call `z64->attr64(1)` or `z64->attr64(0)` whenever their own 32-bit field is `0xFFFFFFFF`, with no check that `z64->attr_count()` actually contains index 1 (or even index 0, for a zero-length field). Likewise `CDH::compressed_file_size()` and `CDH::local_header_offset()` compute an `attr_no` purely from which fields are flagged `0xFFFFFFFF` [4](#0-3) [5](#0-4)  and pass it straight to `attr64()`.

`Zip64ExtraField::attr64` itself performs a raw, unchecked array access: [6](#0-5) .

An attacker who controls the bytes of a jar (e.g., a dependency archive, a maven artifact, or any `--sources` input consumed by `singlejar`) can craft a Zip64 extra field with `payload_size = 8` (one 64-bit attribute) but set both `compressed_file_size32_` and `uncompressed_file_size32_` (or `local_header_offset32_`) to `0xFFFFFFFF`. `ExtraField::find` accepts this field as well-formed because its own 12-byte extent (4-byte header + 8-byte payload) fits inside `extra_fields_length()`. But the consuming code then calls `attr64(1)`, reading 8 bytes immediately past the field's declared payload — bytes that belong to whatever data follows in the memory-mapped input file (the next extra field, file data, next header, or, if the entry sits at the very end of the mapped region, past the mapping entirely).

### Impact Explanation
This is a genuine out-of-bounds read of attacker-influenced memory, structurally analogous to the ImageMagick WEBP ABI-version-check over-read: a size/flag value (`0xFFFFFFFF` marker / ABI version) is trusted to imply a fixed-size trailing structure without validating the structure's actual declared size against the number of elements the code is about to index. The read result then flows into `compressed_file_size()`/`uncompressed_file_size()`/`local_header_offset()`, which drive downstream buffer sizing and offset arithmetic in `output_jar.cc`, potentially causing further mis-sized reads/copies (e.g., in `AppendToDirectoryBuffer` or entry decompression paths) or a crash. Since `singlejar` runs as part of ordinary build actions (not sandboxed against malicious jar *content*, only against filesystem access), a hostile dependency can trigger this purely by shipping a crafted jar that a victim's build ingests.

### Likelihood Explanation
Moderate-to-high: any Bazel build that merges externally-sourced jars via `singlejar` (deploy jars, `java_import`, etc.) is exposed, and the malformed-field detection added for the oversized-extra-field case (`CreateZipWithMalformedExtraField` / `MalformedExtraField` test in `output_jar_simple_test.cc`) does not cover this attribute-count/flag mismatch — it only guards against a payload extending past `extra_fields_length()`, not against a payload that is *validly bounded* but *too short* for the number of 0xFFFFFFFF-flagged 32-bit fields referencing it.

### Recommendation
In `Zip64ExtraField`, add a bounds check inside `attr64(int index)` (or at each call site) that verifies `index < attr_count()` before dereferencing `attr_[index]`, and treat a violation as a malformed archive (`diag_errx`), consistent with the existing "malformed extra field" handling in `output_jar.cc`.

### Proof of Concept
Extend the existing `output_jar_simple_test.cc` malformed-extra-field test pattern:
1. Build a jar whose `CDH`/`LH` entry has `compressed_file_size32_ = uncompressed_file_size32_ = 0xFFFFFFFF`.
2. Attach a `Zip64ExtraField` with `payload_size = 8` (a single 8-byte attribute, satisfying only `uncompressed_file_size`).
3. Run `singlejar`/`OutputJar::Doit()` against this crafted input (analogous to `CreateZipWithMalformedExtraField`/`MalformedExtraField`), and observe that `compressed_file_size()` calls `attr64(1)`, reading 8 bytes past the field's declared 12-byte extent — verifiable with ASan as a heap-buffer-overflow read, or by asserting the returned value is garbage instead of triggering the existing "malformed extra field" diagnostic.

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

**File:** src/tools/singlejar/zip_headers.h (L240-247)
```text
  size_t compressed_file_size() const {
    size_t size32 = compressed_file_size32();
    if (ziph::zfield_has_ext64(size32)) {
      const Zip64ExtraField* z64 = zip64_extra_field();
      return z64 == nullptr ? 0xFFFFFFFF : z64->attr64(1);
    }
    return size32;
  }
```

**File:** src/tools/singlejar/zip_headers.h (L255-262)
```text
  size_t uncompressed_file_size() const {
    size_t size32 = uncompressed_file_size32();
    if (ziph::zfield_has_ext64(size32)) {
      const Zip64ExtraField* z64 = zip64_extra_field();
      return z64 == nullptr ? 0xFFFFFFFF : z64->attr64(0);
    }
    return size32;
  }
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
