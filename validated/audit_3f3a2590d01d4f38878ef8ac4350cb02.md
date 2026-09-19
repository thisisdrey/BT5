## Title
Unchecked Zip64 extra-field attribute index causes heap out-of-bounds read of untrusted archive data - (File: `src/tools/singlejar/zip_headers.h`)

### Summary
`Zip64ExtraField::attr64(int index)` reads a fixed-size array (`attr_[]`) that is only as large as `payload_size()/8` entries, but every caller that computes an "attribute number" from the 32-bit size/offset placeholder fields (`0xFFFFFFFF` sentinel) never checks that the derived index is `< attr_count()` before calling `attr64(index)`. This mirrors the CVE-2016-10253 bug class: a value taken from attacker-controlled input (which fields are flagged as 64-bit) is used directly as an unvalidated array index/offset into a buffer, permitting an out-of-bounds read of adjacent heap memory.

### Finding Description
`ExtraField::find` (used by `Zip64ExtraField::find`) validates only that the *Zip64 extra field record itself* (4-byte header + `payload_size()` bytes) lies within the bounds of the entry's declared `extra_fields_length()`: [1](#0-0) 

However, `attr64()` performs no bounds check against `attr_count()`: [2](#0-1) 

Callers derive the attribute index purely from which 32-bit fields in the Local/Central header equal the `0xFFFFFFFF` sentinel — a value fully controlled by the attacker crafting the archive — and then blindly index into `attr_`:

- `LH::compressed_file_size()` / `uncompressed_file_size()` call `attr64(1)` / `attr64(0)` whenever the corresponding 32-bit field is `0xFFFFFFFF`, without checking `attr_count()`: [3](#0-2) 

- `CDH::local_header_offset()` computes `attr_no` by summing up to two boolean flags (`uncompressed_file_size32()==0xFFFFFFFF`, `compressed_file_size32()==0xFFFFFFFF`) and calls `z64->attr64(attr_no)` with no check that `attr_no < zip64_ef->attr_count()`: [4](#0-3) 

An attacker can craft a ZIP/JAR entry where:
1. `uncompressed_file_size32`/`compressed_file_size32`/`local_header_offset32` are set to `0xFFFFFFFF` (triggering the ext64 path), while
2. the accompanying Zip64 extra field is present (so `zip64_extra_field()` doesn't return `nullptr`) but declares `payload_size = 0` (or a small value), so `attr_count()` is 0 or 1.

This satisfies the `ExtraField::find` boundary check (the 4-byte-or-slightly-larger record fits within `extra_fields_length()`), yet the caller still indexes `attr_[0]`, `attr_[1]`, or `attr_[attr_no]`, reading 8–24 bytes past the validated field boundary — into whatever heap/mmap-adjacent memory follows (the filename bytes, comment, next central directory entry, or, if the extra field sits at the tail of the `mmap`'d file, unmapped memory).

The resulting garbage 64-bit values are then used as authoritative sizes/offsets:
- `local_header_offset()` flows into `InputJar::LocalHeader()`, which does `mapped_file_.address(cdh->local_header_offset() + preamble_size_)` — an attacker-influenced (partially garbage) file offset used to compute a pointer into the mapped input.
- `compressed_file_size()`/`uncompressed_file_size()` flow into copy/compress operations in `output_jar.cc` (e.g., `num_bytes += lh->compressed_file_size();` and subsequent `WriteBytes(input_jar.mapped_start() + copy_from, num_bytes)`), i.e. a heap-adjacent-memory-derived offset/size directly drives file I/O and buffer copies: [5](#0-4) [6](#0-5) 

### Impact Explanation
`singlejar` (`src/tools/singlejar/*`) is Bazel's own tool for merging/repackaging JARs, invoked as part of ordinary `java_binary`/`java_library` build actions to combine dependency jars — including third-party artifacts fetched from remote Maven repositories via `rules_jvm_external`/`maven_install` or similar external rules. An attacker who controls the bytes of a dependency JAR (a hostile artifact server, or a compromised/unpinned mirror) can trigger a heap out-of-bounds read whose result (garbage bytes reinterpreted as sizes/offsets) drives subsequent memory copies and file writes performed by `OutputJar`. Depending on what adjacent memory holds, this can crash the build process (denial of service via SIGSEGV on an unmapped page) or leak adjacent heap bytes into the produced output jar (information disclosure of build-host memory content), and in the worst case corrupt the copy length/offset enough to attempt large out-of-bounds reads/writes during `WriteBytes`.

### Likelihood Explanation
Exploitability requires only crafting a malformed ZIP/JAR (setting the 32-bit sentinel fields but supplying a truncated Zip64 extra field) — no privileged access, no MITM, and no reliance on cryptographic bypass. The condition is reachable any time `singlejar` (or `ijar`, which has a structurally similar unchecked pattern in `third_party/ijar/zip.cc`) processes a jar originating from an external/untrusted source such as a compromised dependency mirror serving an unexpected artifact for a given coordinate/version (bzlmod/Maven resolution does not always cryptographically pin every transitive artifact's ZIP structure — sha256/integrity checks apply to the whole file, but a hostile origin that matches the pinned hash trivially defeats this by construction, and any workflow lacking a pin is fully exposed). The vulnerable code paths (`LH`/`CDH` accessors) are exercised on every single jar entry processed, making the trigger condition simple and reliable to hit once a hostile jar reaches the build.

### Recommendation
Add bounds validation before every `attr64()` call: verify `index < attr_count()` (equivalently `payload_size() >= (index + 1) * sizeof(uint64_t)`) inside `Zip64ExtraField::attr64()` itself (or at each call site in `LH::compressed_file_size()`, `LH::uncompressed_file_size()`, and `CDH::local_header_offset()`/`CDH::compressed_file_size()`/`CDH::uncompressed_file_size()`), and treat a failed check as a malformed-archive error (`diag_errx`) rather than silently returning attacker-influenced garbage. Apply the equivalent fix to the analogous unchecked extra-field parsing in `third_party/ijar/zip.cc`'s `ProcessCentralDirEntry`, which also trusts `data_size`-bounded reads without cross-checking sentinel/attribute consistency.

### Proof of Concept
A reproducible JUnit/C++ test (extending `src/tools/singlejar/output_jar_simple_test.cc`, alongside the existing `MalformedExtraField` test) should:
1. Build a synthetic ZIP/JAR whose Local Header and Central Directory Header set `uncompressed_file_size32` and `compressed_file_size32` to `0xFFFFFFFF`.
2. Attach a Zip64 extra field (tag `0x0001`) with `payload_size = 0` (zero attributes) immediately followed by only a few bytes of filler before the end of the mapped file (or before the next header), so that `attr64(0)`/`attr64(1)` reads past the field's validated payload into adjacent memory or past the mmap boundary.
3. Run this input through `OutputJar::AddJar`/`InputJar::NextEntry` and assert either a crash (ASan heap-buffer-overflow/SEGV) or that `compressed_file_size()`/`local_header_offset()` return non-deterministic/garbage values instead of failing cleanly, analogous to the existing `ASSERT_DEATH(output_jar.Doit(), "malformed extra field")` pattern used for other malformed-field tests: [7](#0-6)

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

**File:** src/tools/singlejar/zip_headers.h (L240-266)
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

**File:** src/tools/singlejar/output_jar.cc (L593-609)
```text
    // Now we have to copy:
    //  local header
    //  file data
    //  data descriptor, if present.
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

**File:** src/tools/singlejar/output_jar.cc (L666-671)
```text
    // Do the actual copy.
    if (!WriteBytes(input_jar.mapped_start() + copy_from, num_bytes)) {
      diag_err(1, "%s:%d: Cannot write %zu bytes of %.*s from %s", __FILE__,
               __LINE__, num_bytes, file_name_length, file_name,
               input_jar_path.c_str());
    }
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
