### Title
Out-of-bounds read of Zip64 extra-field attributes in singlejar's `CDH`/`LH` accessors due to unchecked attribute index - (File: `src/tools/singlejar/zip_headers.h`)

### Summary
`Zip64ExtraField::attr64(int index)` performs raw indexing into a flexible array member with no bounds check against the field's own declared attribute count. The call sites in `CDH::compressed_file_size()`, `CDH::uncompressed_file_size()`, `CDH::local_header_offset()`, and `LH::compressed_file_size()/uncompressed_file_size()` derive the index purely from which 32-bit size/offset fields are marked `0xFFFFFFFF`, never checking that the actual Zip64 extra field is large enough to contain that many 8-byte attributes.

### Finding Description
`ExtraField::find()` only validates that the extra field's *own* declared `payload_size()` fits inside the surrounding `extra_fields` buffer [1](#0-0)  — it never validates that the payload is large enough for the number of 64-bit attributes the caller is about to request. `Zip64ExtraField::attr_count()` computes the number of attributes from `payload_size()`, but nothing forces callers to check it before calling `attr64(index)` [2](#0-1) .

The consuming accessors compute `index` from unrelated 32-bit fields rather than from `attr_count()`:
- `CDH::local_header_offset()` computes `attr_no` by checking whether `uncompressed_file_size32()` and `compressed_file_size32()` are `0xFFFFFFFF`, then calls `z64->attr64(attr_no)` unconditionally [3](#0-2) .
- `CDH::compressed_file_size()` similarly calls `attr64(ziph::zfield_has_ext64(uncompressed_file_size32()))` [4](#0-3) .
- `LH::compressed_file_size()`/`uncompressed_file_size()` call `attr64(1)`/`attr64(0)` unconditionally whenever the corresponding 32-bit field is `0xFFFFFFFF` [5](#0-4) .

An attacker who controls the bytes of a jar/zip that Bazel's `singlejar` tool ingests (e.g., a dependency jar served from a hostile URL/registry and referenced as an input to a `deploy_jar`/`singlejar` action) can set any subset of `compressed_file_size32`, `uncompressed_file_size32`, `local_header_offset32` to `0xFFFFFFFF` while attaching a Zip64 extra field whose declared `payload_size` is smaller than what the corresponding accessor requires (e.g., only 8 bytes / 1 attribute, but the code needs `attr64(2)`, 16 bytes further in). Since `find()` only checks the field boundary equals its own declared size — not that the size matches the number of "ext64" flags set on the surrounding 32-bit fields — the mismatch is never detected, and `attr64()` reads memory past the extra field's payload (and potentially past the end of the mapped jar file/buffer for entries near the end of the memory-mapped input). This is structurally the same bug class as CVE-2023-49100: an attacker-controlled selector value is used to pick an offset into a data structure without validating that the selector is within the bounds the data structure actually declares.

The value read out-of-bounds subsequently feeds into `InputJar::LocalHeader()`, which uses it to compute a pointer via `mapped_file_.address(cdh->local_header_offset() + preamble_size_)` [6](#0-5) , further amplifying the effect of the OOB-read garbage value into subsequent memory address computation.

### Impact Explanation
This is a heap out-of-bounds read inside the `singlejar` process while processing an untrusted archive supplied as a build input (e.g., a jar fetched from a compromised or attacker-hosted URL/registry consumed via `http_jar`/`http_archive`, matching the "no impact from bare MITM" exclusion is avoided because here the attacker is the legitimate *content publisher*, not a network MITM). A crafted Zip64 extra field can cause `singlejar` to read outside the extra field's declared payload and potentially outside the entry's own header/mapped file region, corrupting downstream size/offset computations, and in the worst case crashing the build tool (segfault) or causing it to interpret unrelated heap bytes as a size/offset. Because the malformed value is only consumed internally to drive further pointer arithmetic (not returned to the user directly), this matches the "no leak, but crash/misbehavior" profile described for the analog CVE.

### Likelihood Explanation
An unprivileged, remote attacker only needs to control the bytes of one jar file that ends up as an input to a Bazel `singlejar`/deploy-jar action — e.g., a dependency artifact hosted at a URL a victim's `WORKSPACE`/`MODULE.bazel` fetches, or a file on an untrusted branch a CI job builds. Bazel's `sha256`/integrity mechanisms for such downloads verify byte-for-byte integrity of the file the attacker chose to publish; they do not (and cannot) validate the internal semantic consistency of a Zip64 extra field, so a deliberately malformed jar passes checksum verification and reaches `singlejar` unmodified. No special privileges, credentials, or trusted-repo Starlark access are required.

### Recommendation
In `Zip64ExtraField`, before any `attr64(index)` call at all use sites, validate `index < attr_count()` (i.e., that `payload_size()` is large enough to hold `(index + 1) * sizeof(uint64_t)` bytes) and treat a failed check as a corrupt-archive error (matching the existing `diag_errx`/`diag_warnx` error paths already used elsewhere in `input_jar.cc`), rather than reading past the field.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/tools/singlejar/*_test.cc`-style reproduction:
1. Build a minimal Local Header + Central Directory Header entry where `compressed_file_size32`, `uncompressed_file_size32`, and `local_header_offset32` are all set to `0xFFFFFFFF`.
2. Attach a Zip64 extra field (tag `0x0001`) with `payload_size = 8` (a single 64-bit attribute) even though three ext64-flagged fields require `attr64(0)`, `attr64(1)`, and `attr64(2)` (24 bytes) to be answered correctly.
3. Place this crafted entry near the end of the memory-mapped buffer (as in the existing `CreateZipWithMalformedExtraField` pattern already present in `output_jar_simple_test.cc` [7](#0-6) , but targeting `local_header_offset()`/`compressed_file_size()` accessors instead).
4. Run this through `InputJar::Open`/`NextEntry` and call `CDH::local_header_offset()` — observe (under ASan) a heap-buffer-overflow read past the Zip64 extra field's declared payload.

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

**File:** src/tools/singlejar/zip_headers.h (L240-262)
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

**File:** src/tools/singlejar/input_jar.h (L94-97)
```text
  const LH* LocalHeader(const CDH* cdh) const {
    return reinterpret_cast<const LH*>(
        mapped_file_.address(cdh->local_header_offset() + preamble_size_));
  }
```

**File:** src/tools/singlejar/output_jar_simple_test.cc (L1179-1225)
```text
std::string CreateZipWithMalformedExtraField() {
  std::string zip_data;
  const std::string filename = "evil.bin";

  // 1. Local File Header (LFH)
  size_t lh_offset = zip_data.size();
  size_t lh_size = sizeof(LH) + filename.size();
  zip_data.resize(lh_offset + lh_size, 0);
  auto* lh = reinterpret_cast<LH*>(&zip_data[lh_offset]);
  lh->signature();
  lh->version(10);
  lh->file_name(filename.data(), filename.size());

  // 2. Extra field payload containing an oversized payload_size
  uint8_t ef_buffer[8] = {0};
  auto* ef1 = reinterpret_cast<ExtraField*>(ef_buffer);
  ef1->signature(0x000d);
  ef1->payload_size(0);

  auto* ef2 = reinterpret_cast<ExtraField*>(ef_buffer + ef1->size());
  ef2->signature(0xdead);
  ef2->payload_size(0xf000);  // Malformed size exceeding extra field buffer

  // 3. Central Directory Header (CDH)
  size_t cdh_offset = zip_data.size();
  size_t cdh_size = sizeof(CDH) + filename.size() + sizeof(ef_buffer);
  zip_data.resize(cdh_offset + cdh_size, 0);
  auto* cdh = reinterpret_cast<CDH*>(&zip_data[cdh_offset]);
  cdh->signature();
  cdh->version(20);
  cdh->version_to_extract(10);
  cdh->local_header_offset32(lh_offset);
  cdh->file_name(filename.data(), filename.size());
  cdh->extra_fields(ef_buffer, sizeof(ef_buffer));

  // 4. End of Central Directory (EOCD)
  size_t ecd_offset = zip_data.size();
  zip_data.resize(ecd_offset + sizeof(ECD), 0);
  auto* ecd = reinterpret_cast<ECD*>(&zip_data[ecd_offset]);
  ecd->signature();
  ecd->this_disk_entries16(1);
  ecd->total_entries16(1);
  ecd->cen_size32(cdh_size);
  ecd->cen_offset32(cdh_offset);

  return zip_data;
}
```
