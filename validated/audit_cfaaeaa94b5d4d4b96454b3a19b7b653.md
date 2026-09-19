### Title
Out-of-bounds heap read via unvalidated Zip64 extra-field attribute index in `zip_headers.h` — (File: `src/tools/singlejar/zip_headers.h`)

### Summary
`singlejar`'s Local Header (`LH`) and Central Directory Header (`CDH`) classes compute 64-bit size/offset values by indexing into a `Zip64ExtraField`'s `attr64(index)` array whenever the corresponding 32-bit field equals `0xFFFFFFFF`. The index used (`attr_no`) is derived purely from which 32-bit fields are marked as needing extension, but the code never verifies that the actual `Zip64ExtraField` payload (`attr_count()`) contains that many 64-bit entries before reading `attr64(index)`. An attacker-crafted jar/zip can set the 32-bit fields to `0xFFFFFFFF` while shrinking the accompanying Zip64 extra field's `payload_size`, causing `attr64()` to read memory past the field's declared payload — and potentially past the end of the memory-mapped input file.

### Finding Description
`Zip64ExtraField::attr_count()` and `attr64(index)` perform no bounds checking against each other: [1](#0-0) 

`ExtraField::find` only validates that the *field itself* (its declared `tag_ + payload_size_`) fits inside the `extra_fields` buffer — it does not validate the payload size against how many attributes the consumer expects: [2](#0-1) 

Both `LH` and `CDH` then compute an index from the 32-bit fields' sentinel values and blindly call `attr64(index)`: [3](#0-2) [4](#0-3) [5](#0-4) 

`CDH::compressed_file_size()`/`uncompressed_file_size()` compute the attribute index the same unchecked way: [6](#0-5) 

If a crafted entry declares `compressed_file_size32 == uncompressed_file_size32 == local_header_offset32 == 0xFFFFFFFF` (requiring `attr_no` up to 2) but supplies a `Zip64ExtraField` with `payload_size` covering only 0 or 1 8-byte attributes, `attr64(attr_no)` dereferences `attr_[attr_no]` beyond the field's own memory, which can land beyond the mmap'd input file region entirely (`InputJar` memory-maps the whole file per `src/tools/singlejar/input_jar.h`/`.cc`). This mirrors the CVE-2022-2785 bug class: an unverified, attacker-controlled constant (the "needs-64-bit" sentinel pattern) is used to compute a memory-read location/index into a struct without confirming the struct actually contains data at that offset — "constants... not verified and can point anywhere."

### Impact Explanation
`singlejar` (via `InputJar`/`OutputJar::AddJar`) processes third-party jars supplied as build inputs (e.g., via `http_jar`, `http_archive`, or merged `deps` jars) whose exact bytes are pinned only by a `sha256`/`integrity` hash — the hash binds to the malicious bytes, not to structural safety. Reading past the declared Zip64 extra-field payload can leak adjacent heap/mmap memory contents (an out-of-bounds/arbitrary local memory read, matching the CVE's "arbitrary memory read" impact), or crash the `singlejar` process if the read crosses the mapped file boundary (SIGSEGV, denial of service as a byproduct but the primary class here is the OOB read itself).

### Likelihood Explanation
Any user who builds against an externally-hosted jar (even one whose sha256/integrity matches exactly, since the attacker controls the content at publish time) can trigger this by crafting a zip entry with mismatched size-sentinel fields and an undersized Zip64 extra field. No privileged access or MITM is required — the attacker only needs to serve/publish the file that a victim's `http_archive`/`http_jar`/dependency declaration consumes.

### Recommendation
Add bounds validation before any `attr64(index)` access: verify `Zip64ExtraField::attr_count()` is large enough for the maximum `attr_no` implied by which 32-bit fields are `0xFFFFFFFF`, in `LH::compressed_file_size()`, `LH::uncompressed_file_size()`, `CDH::compressed_file_size()`, `CDH::uncompressed_file_size()`, and `CDH::local_header_offset()`. On failure, treat the entry as corrupt and abort/error out (as is already done elsewhere in `input_jar.cc` for malformed directory records), rather than reading uninitialized/out-of-bounds memory.

### Proof of Concept
A JUnit/`zip_headers_test.cc`-style reproduction: construct a `CDH` where `compressed_file_size32`, `uncompressed_file_size32`, and `local_header_offset32` are all set to `0xFFFFFFFF` (requiring 3 Zip64 attributes, `attr_no` up to 2), but attach a `Zip64ExtraField` with `payload_size(8)` (only 1 attribute, `attr_count()==1`). Calling `cdh->local_header_offset()` computes `attr_no == 2` and invokes `z64->attr64(2)`, reading 8 bytes beyond the field's declared/allocated payload — extend the existing `ZipHeadersTest.CentralDirectoryHeader` test (`src/tools/singlejar/zip_headers_test.cc`) with an ASan-instrumented build/small buffer to observe the out-of-bounds read (e.g., allocate the `CDH`+extra-field bytes in a heap buffer sized exactly to the declared fields so ASan flags the overread), or embed such a crafted entry in a jar consumed by `InputJar`/`OutputJar::AddJar` and run under AddressSanitizer to confirm the heap-buffer-overflow (read).

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

**File:** src/tools/singlejar/zip_headers.h (L159-175)
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
```

**File:** src/tools/singlejar/zip_headers.h (L240-253)
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

**File:** src/tools/singlejar/zip_headers.h (L406-416)
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
