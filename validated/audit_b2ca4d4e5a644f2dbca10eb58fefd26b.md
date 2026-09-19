### Title
Zip64 extra-field attribute index is read without validating `attr_count()`, allowing an out-of-bounds read past a crafted Zip64 extra field - (File: `src/tools/singlejar/zip_headers.h`)

### Summary
`CDH::compressed_file_size()`, `CDH::uncompressed_file_size()`, and `CDH::local_header_offset()` derive the *index* into a `Zip64ExtraField`'s `attr_[]` array from which 32-bit fields are set to the `0xFFFFFFFF` sentinel, but never check that the field's actual `attr_count()` (derived from its own attacker-controlled `payload_size()`) is large enough to contain that index — the same class of bug as the reported kernel issue, where a presence-bit-driven cursor/index into a sparse map is trusted without confirming the referenced slot is actually present in the validated buffer.

### Finding Description
`Zip64ExtraField` stores up to three 64-bit values (uncompressed size, compressed size, local header offset), in a fixed order, but only for the fields whose corresponding 32-bit value in `CDH`/`LH` equals `0xFFFFFFFF` [1](#0-0) . `Zip64ExtraField::attr_count()` is computed purely from the field's own `payload_size()` [2](#0-1) , i.e. it is fully attacker-controlled content from the parsed jar.

`CDH::local_header_offset()` computes `attr_no` from the presence sentinels in the 32-bit `uncompressed_file_size32()`/`compressed_file_size32()` fields and then calls `z64->attr64(attr_no)` directly, with no check that `attr_no < z64->attr_count()`: [3](#0-2) 

`CDH::compressed_file_size()` similarly indexes `attr64(ziph::zfield_has_ext64(uncompressed_file_size32()))` without bounds checking against `attr_count()` [4](#0-3) , and `CDH::uncompressed_file_size()`/`LH::compressed_file_size()`/`LH::uncompressed_file_size()` follow the same unchecked-index pattern [5](#0-4) [6](#0-5) .

The only bounds validation performed is `ExtraField::find()`, which checks that the *declared* field (`sizeof(ExtraField) + payload_size()`) fits within the CDH's `extra_fields` region [7](#0-6) . That check validates the field's own declared extent, not that the number of populated 64-bit attributes matches the number of "present" bits implied by the 32-bit sentinel values — exactly the presence/consumption mismatch in the kernel report. An attacker can set all three 32-bit fields to `0xFFFFFFFF` (implying 3 slots needed) while declaring a `Zip64ExtraField` with `payload_size = 8` (only 1 slot present, `attr_count() == 1`). `ExtraField::find` accepts this field as well-formed (it is 8 bytes and fits), but `local_header_offset()` will then compute `attr_no = 2` and read `attr64(2)`, 16 bytes past the single valid attribute — landing in whatever bytes follow in the memory-mapped central directory (next CDH entry, comment bytes, or, for the last entry, past the mapped region entirely).

### Impact Explanation
The value returned by `local_header_offset()` is used directly as `copy_from`, the source offset for copying local-header + file-data bytes into the merged deploy jar [8](#0-7) , and `compressed_file_size()`/`uncompressed_file_size()` similarly drive size/length computations elsewhere in `output_jar.cc` and `AppendToDirectoryBuffer`. A garbage/out-of-bounds 64-bit value read here can: (a) crash the singlejar process (read past the mmap'd file causing SIGSEGV — a build failure/DoS on that node, which the ruleset here treats as out of scope on its own), or (b) if the fabricated offset happens to land within the mapped file, cause singlejar to copy attacker-uncontrolled adjacent heap/mmap bytes into the output artifact, or corrupt size accounting used for subsequent output writes (`ReserveCdr`, `WriteFileSizeInLocalFileHeader`), producing a corrupted/incoherent deploy jar. This is an out-of-bounds read reachable purely from crafted bytes in a jar consumed as a build input (e.g., a java dependency artifact), landing squarely in the "extraction and parsing of untrusted archive content" surface named in scope.

### Likelihood Explanation
Likelihood is moderate-to-low in practice: reaching this path requires the input jar's CDH to use the Zip64 extension with an internally-inconsistent attribute count relative to the sentinel flags, which is unusual but trivially constructible by an attacker who controls the bytes of a dependency jar (e.g. a maven artifact fetched by URL, or an input jar produced by an untrusted build step). No existing check in `ExtraField::find`, `LocateCentralDirectory`, or `output_jar.cc` validates `attr_count()` against the sentinel-derived index before dereferencing `attr64()`. `output_jar_simple_test.cc`'s `MalformedExtraField` test only checks that a field whose *declared size* exceeds the CDH region is rejected (`diag_errx(1, "malformed extra field")`) — it does not cover the case of a self-consistent, in-bounds field with fewer attributes than the sentinel bits imply [9](#0-8) .

### Recommendation
In `Zip64ExtraField::attr64()` accessors (or at each call site in `CDH`/`LH`), validate `index < attr_count()` before dereferencing, and treat a field whose `attr_count()` is smaller than the number of sentinel-flagged 32-bit fields as malformed (fail closed, consistent with the existing `diag_errx(1, "malformed extra field ...")` handling used for other structural violations).

### Proof of Concept
Construct a minimal jar whose single CDH entry has:
- `compressed_file_size32 = uncompressed_file_size32 = local_header_offset32 = 0xFFFFFFFF`
- A `Zip64ExtraField` (tag `0x0001`) with `payload_size = 8` (i.e. `attr_count() == 1`, containing only one 64-bit value)

Feed this jar to `singlejar --sources bad.jar --output out.jar` (as exercised by the existing `OutputJarSimpleTest` fixtures in `src/tools/singlejar/output_jar_simple_test.cc`, following the same pattern as `MalformedExtraField`/`CreateZipWithMalformedExtraField`). Under ASan/valgrind, `CDH::local_header_offset()`'s call to `z64->attr64(attr_no)` (with `attr_no == 2`) reads 16 bytes past the single declared 64-bit attribute, demonstrating the out-of-bounds read; a `JUnit`/C++ gtest analogous to `ZipHeadersTest.Zip64ExtraFieldTest` in `src/tools/singlejar/zip_headers_test.cc` can assert the OOB directly by constructing such a field and calling `attr64()` at an index beyond `attr_count()`.

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

**File:** src/tools/singlejar/zip_headers.h (L138-148)
```text
/* Zip64 Extra Field (section 4.5.3 of the .ZIP format spec)
 *
 * It is present if a value of a uncompressed_size/compressed_size/file_offset
 * exceeds 32 bits. It consists of a 4-byte header followed by
 * [64-bit uncompressed_size] [64-bit compressed_size] [64-bit file_offset]
 * Only the entities whose value exceed 32 bits are present, and the present
 * ones are always in the order shown above. The originating 32-bit field
 * contains 0xFFFFFFFF to indicate that the value is 64-bit and is in
 * Zip64 Extra Field. Section 4.5.3 of the spec mentions that Zip64 extra field
 * of the Local Header MUST have both uncompressed and compressed sizes present.
 */
```

**File:** src/tools/singlejar/zip_headers.h (L163-165)
```text
  // Attribute count
  int attr_count() const { return payload_size() / sizeof(attr_[0]); }
  void attr_count(int n) { payload_size(n * sizeof(attr_[0])); }
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

**File:** src/tools/singlejar/output_jar.cc (L593-597)
```text
    // Now we have to copy:
    //  local header
    //  file data
    //  data descriptor, if present.
    int64_t copy_from = jar_entry->local_header_offset();
```

**File:** src/tools/singlejar/output_jar_simple_test.cc (L1227-1236)
```text
TEST_F(OutputJarSimpleTest, MalformedExtraField) {
  string out_path = OutputFilePath("out.jar");
  string bad_jar = OutputFilePath("malformed.jar");
  ASSERT_TRUE(
      blaze_util::WriteFile(CreateZipWithMalformedExtraField(), bad_jar));

  ParseCommandLine(out_path, {"--sources", bad_jar});
  OutputJar output_jar(&options_);
  ASSERT_DEATH(output_jar.Doit(), "malformed extra field");
}
```
