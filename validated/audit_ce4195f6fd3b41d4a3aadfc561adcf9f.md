### Title
Zip64 extra-field attribute index read without validating `attr_count()` — (File: `src/tools/singlejar/zip_headers.h`)

### Summary
`singlejar`'s ZIP header parsing computes the index into a Zip64 extended-information extra field's 64-bit attribute array purely from *other* fields' `0xFFFFFFFF` sentinel flags, and never checks that the Zip64 extra field actually declares enough attributes (`attr_count()`) to cover that index before dereferencing it. This is the same bug class as CVE-2026-31611: a count/flag field is used to decide "there must be a 3rd element" but the code never confirms the backing array is that long before reading it.

### Finding Description
`Zip64ExtraField::attr64(int index)` performs a raw, unchecked array read: [1](#0-0) 

`attr_count()` exists specifically to report how many 64-bit values the field actually holds, but none of the call sites consult it before indexing:

- `LH::compressed_file_size()` unconditionally reads `attr64(1)` whenever the 32-bit `compressed_file_size32` field is `0xFFFFFFFF`, regardless of how many attributes the Zip64 field actually declares: [2](#0-1) 

- `CDH::compressed_file_size()` / `CDH::local_header_offset()` compute the index (`attr_no`) from whichever combination of `uncompressed_file_size32`/`compressed_file_size32` are marked as ext64, and can request index 0, 1, or 2: [3](#0-2) [4](#0-3) 

The only bounds check performed anywhere is in `ExtraField::find()`, which validates that the extra field's own declared `size()` (`sizeof(ExtraField) + payload_size()`) fits inside the enclosing `extra_fields` buffer — it says nothing about whether `payload_size()` is large enough to contain the *specific* attribute index the caller is about to read: [5](#0-4) 

So an attacker who controls a JAR/ZIP file can set, e.g., `compressed_file_size32 = 0xFFFFFFFF` and `uncompressed_file_size32 = 0xFFFFFFFF` (both "needs 64-bit value") while attaching a Zip64 extra field whose `payload_size` is only 8 bytes (i.e., `attr_count() == 1`, holding only the uncompressed size). `compressed_file_size()`/`local_header_offset()` will then read `attr_[1]` or `attr_[2]`, which is out-of-bounds relative to the field's declared payload — bytes belonging to whatever data (another extra field, the file name of the next central-directory entry, or heap memory past the mapped/allocated buffer) happen to follow.

The resulting bogus value is not merely informational — it directly drives how many bytes `singlejar` copies out of the input archive when repacking a JAR: [6](#0-5) 

Because this poisoned `compressed_file_size()`/`local_header_offset()` becomes `num_bytes`/`copy_from` used to copy raw bytes from the source archive into the output deploy jar, a corrupted (out-of-bounds-derived) size or offset can cause `singlejar` to read far past the intended entry — either crashing the process or splicing adjacent heap/file bytes into the output artifact.

This mirrors the kernel bug precisely: a flag/prefix condition ("size32 == 0xFFFFFFFF", analogous to SID prefix match) is used to justify reading a specific array slot, but the actual declared length of the backing array (`attr_count()`, analogous to `num_subauth`) is never checked first.

### Impact Explanation
JAR inputs to `singlejar` commonly originate from external, network-fetched artifacts (e.g., Maven/http_jar/http_archive dependencies pinned only by `sha256`). A `sha256` pin verifies byte-for-byte integrity of the exact file an attacker chose to publish — it does not validate the internal structural consistency of that ZIP file. An attacker who controls a dependency's published bytes (and its accompanying/attacker-supplied `sha256`, which the victim copies into their `MODULE.bazel`/`WORKSPACE` when first adding the dependency) can craft a ZIP with an inconsistent Zip64 extra field as described. When Bazel later builds a `deploy.jar` (or otherwise merges JARs) using `singlejar`, the out-of-bounds read is triggered inside the build, potentially corrupting the size/offset used for a raw memory copy — this can crash the build (denial of service is explicitly out of scope per the rules) or, more seriously, cause bytes adjacent to the extra-field buffer (which may include heap contents beyond the mapped file) to be spliced into the resulting output jar, an out-of-bounds read whose result is written into the build output.

### Likelihood Explanation
Reaching this code requires a crafted ZIP (Zip64 extra field with `attr_count()` inconsistent with the 32-bit size sentinels) to be supplied as `singlejar` input, which happens for any external JAR dependency processed via `java_binary`/deploy-jar assembly or similar merging rules. Crafting such a file is straightforward with any ZIP-writing library, and no client-side validation exists to reject the inconsistency before it reaches `attr64()`.

### Recommendation
Add an explicit check that `attr_count()` covers the requested index before calling `attr64()` in `LH::compressed_file_size()`, `LH::uncompressed_file_size()`, `CDH::compressed_file_size()`, `CDH::uncompressed_file_size()`, and `CDH::local_header_offset()` (mirroring `Zip64ExtraField::attr_count()`), and treat an insufficiently-sized Zip64 extra field as a malformed/corrupt archive error rather than silently reading past it.

### Proof of Concept
Construct a ZIP central-directory entry (or local header) where:
1. `uncompressed_file_size32 = 0xFFFFFFFF` and `compressed_file_size32 = 0xFFFFFFFF` (both marked as requiring 64-bit values).
2. Attach a Zip64 extra field (tag `0x0001`) with `payload_size = 8` (i.e., only one 8-byte attribute present, `attr_count() == 1`), immediately followed by other bytes/extra fields.
3. Feed this archive into `singlejar` as an input jar (e.g., via `OutputJar::AddJar`).

Expected: `CDH::compressed_file_size()`/`LH::compressed_file_size()` (or `CDH::local_header_offset()`) will call `Zip64ExtraField::attr64(1)` (or `attr64(2)`), reading 8 bytes past the field's declared single-attribute payload, producing a bogus size/offset that is then used to control the byte-copy length/source in `OutputJar::AddJar` (`src/tools/singlejar/output_jar.cc:597-609`). This can be verified with a `zip_headers_test.cc`-style unit test asserting that `attr64()`/the size accessors reject or bound-check an index that exceeds a constructed field's `attr_count()`, analogous to the existing `Zip64ExtraFieldTest` in [7](#0-6) .

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

**File:** src/tools/singlejar/output_jar.cc (L597-609)
```text
    int64_t copy_from = jar_entry->local_header_offset();
    size_t num_bytes = lh->size();
    if (jar_entry->no_size_in_local_header()) {
      const DDR* ddr = reinterpret_cast<const DDR*>(
          lh->data() + jar_entry->compressed_file_size());
      num_bytes +=
          jar_entry->compressed_file_size() +
          ddr->size(
              ziph::zfield_has_ext64(jar_entry->compressed_file_size32()),
              ziph::zfield_has_ext64(jar_entry->uncompressed_file_size32()));
    } else {
      num_bytes += lh->compressed_file_size();
    }
```

**File:** src/tools/singlejar/zip_headers_test.cc (L360-373)
```text
  TEST(ZipHeadersTest, Zip64ExtraFieldTest) {
    uint8_t bytes[256];
    memset(bytes, kPoison, sizeof(bytes));
    Zip64ExtraField* z64 = reinterpret_cast<Zip64ExtraField*>(bytes);

    z64->signature();
    EXPECT_TRUE(z64->is());
    z64->payload_size(16);
    z64->attr64(0, 9876543210);
    EXPECT_EQ(9876543210UL, z64->attr64(0));
    z64->attr64(1, 8976543210);
    EXPECT_EQ(8976543210UL, z64->attr64(1));
    EXPECT_EQ(kPoison, bytes[z64->size()]);
  }
```
