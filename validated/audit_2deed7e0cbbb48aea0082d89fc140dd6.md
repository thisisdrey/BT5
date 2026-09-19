### Title
Missing bounds check when resolving Zip64 extra-field attribute index in `CDH`/`LH` allows out-of-bounds read on attacker-supplied jar/zip input — (`File: src/tools/singlejar/zip_headers.h`)

### Summary
`CDH::compressed_file_size()`, `CDH::local_header_offset()`, `LH::compressed_file_size()` and `LH::uncompressed_file_size()` derive an index into the variable-length `Zip64ExtraField::attr_` array purely from the "is this field `0xFFFFFFFF`" flags on the 32-bit size/offset fields, and then call `attr64(index)` with **no check that the extra field actually contains that many 64-bit attributes**. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
The Zip64 spec (and the comment in this file) states the extra field stores only the 64-bit values that are actually needed, always in the fixed order `[uncompressed][compressed][offset]`, and each 32-bit field that "needs" the extension signals this by containing the sentinel `0xFFFFFFFF`: [4](#0-3) 

Bazel's singlejar computes which slot in the Zip64 extra field to read by simply testing the 32-bit fields for the `0xFFFFFFFF` sentinel and incrementing a counter — e.g., in `CDH::local_header_offset()`: [2](#0-1) 

`Zip64ExtraField::attr64(int index)` simply indexes into the flexible array member `attr_[]` with no bounds validation against `attr_count()` (which is derived from `payload_size()`, the field length declared by the (attacker-controlled) zip entry itself): [3](#0-2) 

`ExtraField::find()` only validates that the extra-field *header* and its self-declared `size()` fit within the surrounding `extra_fields()`/`extra_fields_length()` byte range — it does not, and cannot, know how many 64-bit attributes the *consumer* is about to read: [5](#0-4) 

Consequently, an attacker who controls the bytes of a `.zip`/`.jar` file that singlejar merges (via `output_jar.cc`, which calls `zip64_extra_field()`/`local_header_offset()`/`compressed_file_size()` while building a deploy jar from `--sources` inputs) can craft a Central Directory Header where:
- `uncompressed_file_size32_ == 0xFFFFFFFF` and/or `compressed_file_size32_ == 0xFFFFFFFF` and/or `local_header_offset32_ == 0xFFFFFFFF` (forcing a larger `attr_no`), while
- the attached Zip64 extra field's `payload_size_` (hence `attr_count()`) declares fewer 64-bit attributes than the computed index requires (e.g., `payload_size_ = 8` → `attr_count() == 1`, but `attr_no` computed as 2).

`attr64(attr_no)` then reads 8-byte-aligned data past the end of the extra field's declared payload. Since the extra field's payload length is only checked against the *surrounding* `extra_fields` buffer (which itself is sized from `extra_fields_length()`, also attacker-controlled and could be crafted to be just barely large enough to pass `ExtraField::find`'s check but still leave the read past validly-populated bytes), this is effectively reading attacker-adjacent bytes in the mapped input buffer as a 64-bit size/offset value with no sentinel or overflow check to reject it. This mirrors the Xen CVE's root cause: an incorrect/insufficient check ("mask") used to gate access based on an overflow indicator, letting the code trust a value that was never actually validated to be present.

This value is subsequently used as `compressed_file_size()`/`uncompressed_file_size()`/`local_header_offset()`, i.e., as a length or offset into the mmap'd input file for decompression (`transient_bytes.h::DecompressEntryContents`) and file positioning — feeding an out-of-bounds-read-derived value into subsequent size/offset-driven memory operations.

### Impact Explanation
This is a genuine memory-safety defect (heap/mmap out-of-bounds read) reachable purely by supplying a crafted jar/zip as a build input — e.g., a `http_jar`/`http_archive`-fetched dependency merged into a `deploy_jar` by singlejar. The attacker only needs to control the *contents* of a file whose sha256/integrity the build already expects to match (the checksum validates the downloaded bytes are unmodified, not that the internal Zip64 structure is well-formed), so a hostile origin/registry publishing a legitimately-checksummed but structurally malicious jar satisfies the threat model. The corrupted size/offset value can crash the singlejar process (denial of service for that build) or, depending on how far out-of-bounds the read lands and how the resulting bogus length is subsequently used for decompression/copy sizing, potentially read adjacent heap memory into the archive's declared file size.

### Likelihood Explanation
Likelihood is moderate: it requires the target to build a deploy/merged jar via singlejar from an external, attacker-influenced jar dependency, and requires crafting a `payload_size_` in the Zip64 extra field that is smaller than what the CDH's `0xFFFFFFFF` sentinel pattern implies — both of which are entirely under the control of whoever produces the jar (no MITM, no compromised host, no privileged access needed).

### Recommendation
In `Zip64ExtraField::attr64()`, `CDH::compressed_file_size()/uncompressed_file_size()/local_header_offset()`, and the equivalent `LH` methods, validate `attr_no < attr_count()` (and that `attr_count() * sizeof(uint64_t) <= payload_size()` is consistent with `extra_fields_length()`) before indexing into `attr_`, and treat a mismatch as a corrupt/rejected zip entry rather than silently reading past the declared attribute array.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell` style repro would: (1) hand-construct a Central Directory Header with `uncompressed_file_size32_ = 0xFFFFFFFF` and `local_header_offset32_ = 0xFFFFFFFF`, but attach a Zip64 extra field with `payload_size_ = 8` (i.e., only one 64-bit attribute, `attr_count() == 1`); (2) feed this to `singlejar --sources <crafted.jar>`; the code path in `CDH::local_header_offset()` computes `attr_no = 1` (since only `uncompressed_file_size32()` flags, but if crafted so `compressed_file_size32()` is *not* flagged, `attr_no` stays at 1 while `attr_count()==1` means index 1 is already out of range for a 1-element array) and calls `z64->attr64(1)`, reading past the declared 8-byte payload. This was not run against a live binary in this analysis; verifying the exact overread magnitude and observable effect (crash vs. silently wrong size) would require building and running `singlejar` (e.g., extending `src/tools/singlejar/zip_headers_test.cc`) against such a crafted structure — noted here as unverified/uncertain due to the ask-only nature of this analysis.

### Citations

**File:** src/tools/singlejar/zip_headers.h (L61-70)
```text
  /* Utility functions to handle Zip64 extensions. Size and position fields in
   * the Zip headers are 32-bit wide. If field's value does not fit into 32
   * bits (more precisely, it is >= 0xFFFFFFFF), the field contains 0xFFFFFFFF
   * and the actual value is saved in the corresponding 64-bit extension field.
   * The first function returns true if there is an extension for the given
   * field value, and the second returns true if given field value needs
   * extension.
   */
  static bool zfield_has_ext64(uint32_t v) { return v == 0xFFFFFFFF; }
  static bool zfield_needs_ext64(uint64_t v) { return v >= 0xFFFFFFFF; }
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

**File:** src/tools/singlejar/zip_headers.h (L159-177)
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

 private:
  uint64_t attr_[];
} attr_packed;
static_assert(4 == sizeof(Zip64ExtraField),
              "Zip64ExtraField class fields layout is incorrect.");
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
