### Title
Out-of-bounds Zip64 extra-field read via mismatched size markers - (File: src/tools/singlejar/zip_headers.h)

### Summary
Confirmed analog exists. `LH::compressed_file_size()`, `LH::uncompressed_file_size()`, `CDH::compressed_file_size()`, and `CDH::local_header_offset()` in singlejar's zip header parser index into a `Zip64ExtraField`'s fixed-size `attr64(index)` array using an index derived from unrelated 32-bit size/offset fields, without checking `attr_count()` against that index — mirroring the CVE-2017-16912 pattern where `get_pipe()` indexed an array using an attacker-influenced value without validating it against the array's actual bounds.

### Finding Description
A zip's Local Header (`LH`) and Central Directory Header (`CDH`) mark oversized 32-bit fields with the sentinel `0xFFFFFFFF` and store real 64-bit values in an appended `Zip64ExtraField`, whose `attr_count()` reflects only `payload_size() / 8` — i.e., exactly as many 64-bit values as the attacker chose to include in the extra field's declared `payload_size`. [1](#0-0) 

The accessors that resolve the real 64-bit values compute an index into `attr_[]` from *which* 32-bit fields are flagged as overflowed, then call `attr64(index)` unconditionally: [2](#0-1) [3](#0-2) [4](#0-3) 

None of these call sites check `z64->attr_count()` before calling `attr64(index)`. For example, `CDH::local_header_offset()` computes `attr_no` from whether `uncompressed_file_size32()` and `compressed_file_size32()` are `0xFFFFFFFF`, and can request `attr_no == 2`, but a crafted `Zip64ExtraField` can declare `payload_size() == 8` (i.e. `attr_count() == 1`), so `attr64(2)` reads 16 bytes past the field's own declared payload. Because `Zip64ExtraField::find()` only bounds-checks the field's *declared* `size()` against the enclosing extra-fields buffer (not against the number of attrs actually required by the flags), this over-read is not caught: [5](#0-4) 

This is architecturally the same bug class as CVE-2017-16912: a length/flag field controlled by untrusted input determines an index used to access a fixed-capacity array/structure, and the code trusts that index without validating it against the structure's actual declared size, yielding an out-of-bounds read.

### Impact Explanation
The read is bounded by the memory-mapped input file/extra-fields buffer size in most cases (heap over-read within or slightly past the mapped input), which can crash `singlejar` (denial of service for the build action) or leak adjacent heap/mmap bytes into computed offsets that subsequently influence file copies (`OutputJar::AddJar`, `WriteEntry`) — e.g., a corrupted `local_header_offset` derived from OOB memory could cause `WriteBytes(input_jar.mapped_start() + copy_from, num_bytes)` to copy from an attacker-uncontrolled but wrong offset, corrupting the output jar. `singlejar` is invoked as part of ordinary Java build actions (e.g., building deploy jars from `srcs`), so a jar/zip file supplied by an attacker-controlled dependency (e.g. an `http_archive` with a crafted `.jar` inside, or a source file in a malicious branch) reaches this parser.

### Likelihood Explanation
Existing tests (`CreateZipWithMalformedExtraField` / `MalformedExtraField` test) show the project is aware of and hardened against malformed/oversized `payload_size` fields (an explicit `diag_errx` fires when `ExtraField`'s declared size exceeds the buffer), but that hardening is for the ExtraField-container overflow case, not for the field-count vs. flag-count mismatch described here. A specifically crafted zip that sets a 32-bit size field to `0xFFFFFFFF` while providing a Zip64 extra field with fewer 64-bit attributes than required is not covered by that check.

### Recommendation
Add an explicit `attr_count()` check in each accessor before calling `attr64(index)` (e.g., `if (z64 == nullptr || attr_no >= z64->attr_count()) { diag_errx / return sentinel with error }`) in `LH::compressed_file_size()`, `LH::uncompressed_file_size()`, `CDH::compressed_file_size()`, `CDH::uncompressed_file_size()`, and `CDH::local_header_offset()`.

### Proof of Concept
Extend `output_jar_simple_test.cc`'s `CreateZipWithMalformedExtraField()` pattern: construct a `LH`/`CDH` pair whose `compressed_file_size32`/`uncompressed_file_size32` fields are both set to `0xFFFFFFFF`, but attach a `Zip64ExtraField` with `payload_size(8)` (only one `uint64` attribute) instead of the two/three normally required. Feed this crafted jar to `OutputJar::Doit()` as in the existing `MalformedExtraField` test and observe that `attr64(1)` (or `attr64(2)` for `local_header_offset`) reads uninitialized/out-of-bounds memory rather than triggering the `"malformed extra field"` diagnostic, which only guards the `ExtraField::find` bounds and not this count mismatch — this can be verified with a `BuildIntegrationTestCase`/gtest asserting either a sanitizer (ASan) failure or a `diag_errx` is required but absent.

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

**File:** src/tools/singlejar/zip_headers.h (L149-177)
```text
class Zip64ExtraField : public ExtraField {
 public:
  static const Zip64ExtraField* find(const uint8_t* start, const uint8_t* end) {
    return reinterpret_cast<const Zip64ExtraField*>(
        ExtraField::find(1, start, end));
  }

  bool is() const { return is_zip64(); }
  void signature() { ExtraField::signature(1); }

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

 private:
  uint64_t attr_[];
} attr_packed;
static_assert(4 == sizeof(Zip64ExtraField),
              "Zip64ExtraField class fields layout is incorrect.");
```

**File:** src/tools/singlejar/zip_headers.h (L240-269)
```text
  size_t compressed_file_size() const {
    size_t size32 = compressed_file_size32();
    if (ziph::zfield_has_ext64(size32)) {
      const Zip64ExtraField* z64 = zip64_extra_field();
      return z64 == nullptr ? 0xFFFFFFFF : z64->attr64(1);
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
    size_t size32 = uncompressed_file_size32();
    if (ziph::zfield_has_ext64(size32)) {
      const Zip64ExtraField* z64 = zip64_extra_field();
      return z64 == nullptr ? 0xFFFFFFFF : z64->attr64(0);
    }
    return size32;
  }
  size_t uncompressed_file_size32() const {
    return le32toh(uncompressed_file_size32_);
  }
  void uncompressed_file_size32(uint32_t v) {
    uncompressed_file_size32_ = htole32(v);
  }

```

**File:** src/tools/singlejar/zip_headers.h (L406-437)
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
  size_t uncompressed_file_size32() const {
    return le32toh(uncompressed_file_size32_);
  }

  void uncompressed_file_size32(uint32_t v) {
    uncompressed_file_size32_ = htole32(v);
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
