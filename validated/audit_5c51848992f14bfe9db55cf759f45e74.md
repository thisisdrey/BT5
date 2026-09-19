## Title
Missing bounds check on Zip64 extra‑field attribute count leads to out‑of‑bounds read when parsing crafted JAR/ZIP entries - (File: `src/tools/singlejar/zip_headers.h`)

### Summary
`singlejar` (used by `java_binary`/`java_import`-style rules to merge all input jars, including jars fetched from external dependencies such as `http_jar`/Maven artifacts) parses ZIP64 extra fields when reading a Central Directory Header (`CDH`). The accessor computes an index into the `Zip64ExtraField`'s variable-length `attr_[]` array from the *32-bit size fields* of the header, but never checks that index against the field's own declared attribute count (`attr_count()`, derived from `payload_size()`). A crafted archive can set the 32-bit `compressed_file_size`/`uncompressed_file_size` markers to `0xFFFFFFFF` while providing a `Zip64ExtraField` with fewer 64-bit attributes than the combination implies, causing `attr64(index)` to read past the field's allocated storage.

### Finding Description
`CDH::compressed_file_size()` and `CDH::uncompressed_file_size()` in `src/tools/singlejar/zip_headers.h` trust the relationship between the 32-bit "needs extension" sentinel (`0xFFFFFFFF`) and the position of the corresponding value inside the `Zip64ExtraField`: [1](#0-0) 

`Zip64ExtraField::attr64(int index)` performs an unchecked array access (`attr_[index]`), and `attr_count()` is only used elsewhere for writing, not for validating reads: [2](#0-1) 

The only containment check performed when locating this extra field is `ExtraField::find`, which validates that the field's *declared* `size()` (header + `payload_size()`) fits within the extra-fields byte range — it says nothing about whether `payload_size()` is large enough to satisfy the specific index that `compressed_file_size()`/`uncompressed_file_size()` will request: [3](#0-2) 

This mirrors the reported bug class exactly: a size/shape value (`payload_size()` → `attr_count()`) is treated as sufficient to bound a coordinate (the attribute `index`) into an underlying buffer, but the code that computes/consumes the index never re-validates it against the actual declared size before calling the low-level, precondition-trusting accessor `attr64()`.

Concretely: if an entry's central-directory header sets `compressed_file_size32 == 0xFFFFFFFF` (meaning "read compressed size from the Zip64 extra field") while `uncompressed_file_size32` is a normal (non-sentinel) 32-bit value, `compressed_file_size()` computes `index = zfield_has_ext64(uncompressed_file_size32()) ? 1 : 0` → `0`, and calls `z64->attr64(0)`. If the attacker crafts the `Zip64ExtraField` with `payload_size() == 0` (i.e. `attr_count() == 0`, no 64-bit values actually present), `attr64(0)` still dereferences `attr_[0]`, reading 8 bytes immediately after the 4-byte extra-field header — bytes that belong to whatever follows in the memory-mapped input file (or past the mapping if the field sits at the very end of the mapped region).

### Impact Explanation
The resulting bogus/out-of-bounds size value is subsequently used to read entry content (`ReadEntryContents`/`DecompressEntryContents` in `src/tools/singlejar/transient_bytes.h`) and to size output buffers, so beyond an out-of-bounds heap read (potential crash / `SIGSEGV`, or leakage of adjacent process memory into the produced jar depending on where the OOB bytes land), it can corrupt the merged jar's contents. Because `singlejar` is invoked during ordinary `java_binary`/`java_import` builds on any jar input — including third-party jars fetched via `http_jar`, `maven_jar`, or similar unprivileged, attacker-served content — this is reachable purely by supplying a malicious jar at a URL a victim's build fetches.

### Likelihood Explanation
An attacker who can publish or serve a jar consumed by a build (a compromised/malicious mirror, or a malicious dependency version at a URL the build fetches without integrity failing first) can craft the ZIP central directory bytes directly; no cooperation from the victim beyond building against that dependency is required. The existing `sha256`/`integrity` checks on `http_jar`/`http_archive` only protect the *whole archive bytes* against tampering after the fact — they do not stop a dependency that is *itself* malicious and legitimately matches its own pinned checksum, so this is not mitigated by Bazel's download integrity checks.

### Recommendation
In `Zip64ExtraField::attr64()` (or at each call site in `CDH`/`LH`), validate `index < attr_count()` before dereferencing `attr_[index]`, and treat a missing/insufficient Zip64 extra field as a corrupt-archive error (as is already done for the `z64 == nullptr` case) rather than performing the unchecked array access.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` style repro: construct a minimal ZIP whose single entry's Central Directory Header sets `compressed_file_size32 = 0xFFFFFFFF`, a normal (non-`0xFFFFFFFF`) `uncompressed_file_size32`, and appends a `Zip64ExtraField` extra-field record with `payload_size = 0` (no 64-bit attributes). Feed this archive as an input jar to `singlejar` (e.g., via a `java_import`/`http_jar` target) and observe `CDH::compressed_file_size()` reading `attr_[0]` out of the field's declared bounds — reproducible as a standalone `zip_headers_test`/`input_jar_test`-style C++ unit test asserting on the (undefined/garbage) value returned, or a crash under ASan.

### Citations

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

**File:** src/tools/singlejar/zip_headers.h (L159-171)
```text
  // The value of i-th attribute
  uint64_t attr64(int index) const { return le64toh(attr_[index]); }
  void attr64(int index, uint64_t v) { attr_[index] = htole64(v); }

  // Attribute count
  int attr_count() const { return payload_size() / sizeof(attr_[0]); }
  void attr_count(int n) { payload_size(n * sizeof(attr_[0])); }

  // Space needed for this field to accommodate n_attr attributes
  static uint16_t space_needed(int n_attrs) {
    return n_attrs > 0 ? sizeof(Zip64ExtraField) + n_attrs * sizeof(uint64_t)
                       : 0;
  }
```

**File:** src/tools/singlejar/zip_headers.h (L406-430)
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
  size_t compressed_file_size32() const {
    return le32toh(compressed_file_size32_);
  }
  void compressed_file_size32(uint32_t v) {
    compressed_file_size32_ = htole32(v);
  }

  size_t uncompressed_file_size() const {
    uint32_t size32 = uncompressed_file_size32();
    if (ziph::zfield_has_ext64(size32)) {
      const Zip64ExtraField* z64 = zip64_extra_field();
      return z64 == nullptr ? 0xFFFFFFFF : z64->attr64(0);
    }
    return size32;
  }
```
