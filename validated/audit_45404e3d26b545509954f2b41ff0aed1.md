Based on the ijar/singlejar ZIP-parsing code, I found a concrete, unguarded out-of-bounds-read pattern that is directly analogous to the reported CWE-125 class (insufficient bounds validation while parsing a length-prefixed field from untrusted binary input).

### Title
Out-of-bounds read when parsing truncated Zip64 extra-field attributes in singlejar/ijar ZIP header parsing - (File: `src/tools/singlejar/zip_headers.h`)

### Summary
`Zip64ExtraField::attr64(int index)` is called from `LH::compressed_file_size()`, `LH::uncompressed_file_size()`, and `CDH::compressed_file_size()`/`uncompressed_file_size()` without first validating that the extra field's declared `payload_size()` actually contains enough bytes for the requested attribute index.

### Finding Description
`ExtraField::find()` only validates that the *entire* extra-field record (header + `payload_size()`) fits within the caller-supplied `[start, end)` window: [1](#0-0) 

It does not validate that `payload_size()` is large enough to hold the number of 64-bit attributes that will actually be read (`attr_count()` is derived only for iteration purposes, and is never compared against the fixed indices used by callers). `Zip64ExtraField::attr64()` simply indexes into the `attr_` flexible array with no bound check: [2](#0-1) 

The unguarded callers are `LH::compressed_file_size()`/`uncompressed_file_size()`: [3](#0-2) 

and the equivalent `CDH` accessors: [4](#0-3) 

If an attacker-crafted local-file-header or central-directory-header declares a Zip64 extra field (`tag == 1`) whose `payload_size()` is smaller than required (e.g. `payload_size = 0`, so the whole extra field is only the 4-byte header and passes the containment check in `find()`), but also sets `compressed_file_size32`/`uncompressed_file_size32` to `0xFFFFFFFF` (the Zip64 escape value), then `compressed_file_size()`/`uncompressed_file_size()` will call `attr64(0)` or `attr64(1)`, reading 8–16 bytes past the validated extent of the extra field — potentially past the end of the mapped/allocated ZIP buffer.

This code is exercised by singlejar (`src/tools/singlejar/output_jar.cc`), which is the tool used to merge dependency JARs (potentially originating from externally fetched artifacts, e.g. `http_jar`/Maven dependencies) into a single deploy JAR, e.g. via `OutputJar::AddJar` reading each entry's `LH`: [5](#0-4) 

### Impact Explanation
A malicious JAR consumed as a build dependency (its bytes controlled entirely by whoever hosts/serves the artifact, with a pinned checksum only guaranteeing byte-for-byte reproducibility, not benignness) can trigger an out-of-bounds heap/mmap read inside the singlejar build tool. Because `output_jar.cc` already implements explicit "malformed extra field" containment checks for the general extra-field walking (as seen in `WriteEntry`/`AppendToDirectoryBuffer`), this specific `attr64()` index-based access path is the one place that check does not cover, since it is reached from the *value accessor* methods (`compressed_file_size()`, etc.) rather than from the extra-field iteration loop that has the size-bounded check.

### Likelihood Explanation
Note: I could not fully confirm at what exact call sites `compressed_file_size()`/`uncompressed_file_size()` (as opposed to the explicit extra-field iteration loops that already sanitize entries) are invoked on attacker-controlled `LH`/`CDH` objects during singlejar processing, nor whether an additional length guard exists elsewhere in `output_jar.cc` that filters out zip64 fields with insufficient payload before these accessors are called. This uncertainty affects confidence in exploitability versus the existing `diag_errx` containment checks for malformed extra fields shown near [6](#0-5) .

### Recommendation
Add an explicit `payload_size()`/`attr_count()` bound check inside `Zip64ExtraField::attr64()` (or at each call site in `LH`/`CDH`) before indexing, treating a Zip64 extra field with insufficient payload as malformed and erroring out the same way `output_jar.cc` already does for other malformed extra-field cases.

### Proof of Concept
A `BuildIntegrationTestCase`/gtest analogous to the existing `CreateZipWithMalformedExtraField()` helper in `output_jar_simple_test.cc` could be extended: construct a Local File Header with `compressed_file_size32 = uncompressed_file_size32 = 0xFFFFFFFF` and a Zip64 extra field (`tag=1`) with `payload_size = 0`, placed at the very end of the mapped buffer, then invoke code paths that call `LH::compressed_file_size()`/`uncompressed_file_size()` and observe the out-of-bounds read (e.g., under ASan) reading past the buffer end. [7](#0-6)

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

**File:** src/tools/singlejar/zip_headers.h (L240-268)
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

**File:** src/tools/singlejar/output_jar.cc (L446-454)
```text
  while ((jar_entry = input_jar.NextEntry(&lh))) {
    const char* file_name = jar_entry->file_name();
    auto file_name_length = jar_entry->file_name_length();
    if (!file_name_length) {
      diag_errx(
          1, "%s:%d: Bad central directory record in %s at offset 0x%" PRIx64,
          __FILE__, __LINE__, input_jar_path.c_str(),
          input_jar.CentralDirectoryRecordOffset(jar_entry));
    }
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
