## Title
Out-of-bounds read via unchecked Zip64 extra-field attribute index in singlejar's `CDH`/`LH` size accessors - (File: `src/tools/singlejar/zip_headers.h`)

### Summary
Bazel's `singlejar` tool (invoked implicitly whenever a `java_library`/`java_binary` target is built, merging `.jar` inputs including third-party jars fetched by the build) parses ZIP Central Directory Headers (`CDH`) and Local Headers (`LH`) directly from an mmap'd, attacker-influenced jar file. When a 32-bit size field is `0xFFFFFFFF`, the code looks up the real 64-bit value in the file's `Zip64ExtraField` payload using an index derived from other untrusted fields, without ever validating that index against the extra field's actual attribute count (`attr_count()`).

### Finding Description
`Zip64ExtraField::attr64(int index)` performs a raw, unchecked array access into the variable-length `attr_` payload: [1](#0-0) 

`CDH::compressed_file_size()` computes the index to pass into `attr64` from a second untrusted 32-bit field (`uncompressed_file_size32()`), rather than from the field's own declared `attr_count()`: [2](#0-1) 

`LH::compressed_file_size()` similarly hard-codes index `1`, and `uncompressed_file_size()`/`LH::uncompressed_file_size()` hard-code index `0`, again without confirming the extra field actually contains that many 8-byte attributes: [3](#0-2) 

The only validation performed on the extra field before this is `ExtraField::find`, which merely ensures the field header/payload fits within the `extra_fields` region — it says nothing about how many 8-byte `attr_` slots are present relative to what the size accessors assume: [4](#0-3) 

An attacker who controls the bytes of a `.jar`/`.zip` archive that ends up as an input to `singlejar` (e.g., a third-party dependency archive fetched via `http_archive`/`http_jar`/`maven_install` and consumed by a Java build, or any jar produced from an untrusted branch's CI) can craft a `CDH`/`LH` entry that:
- Sets `compressed_file_size32` (and/or `uncompressed_file_size32`) to `0xFFFFFFFF` to trigger the Zip64 path.
- Provides a `Zip64ExtraField` with a `payload_size` that yields `attr_count() == 1` (only one 8-byte slot present, as allowed by the spec when only one dimension needs 64-bit extension).
- Sets the *other* size field's high bit condition such that the computed index is `1` instead of `0` (for `CDH`), or relies on `LH`'s hard-coded index `1` for compressed size regardless of how many attributes are actually present.

This causes `attr64(1)` to read 8 bytes starting immediately past the single valid `attr_[0]` slot — i.e., past the end of the extra-field payload that `ExtraField::find` validated, into whatever bytes happen to follow in the mmap'd jar (attacker-controlled adjacent zip bytes, or, near the end of the mapping, unmapped memory). `InputJar` maps the file directly (`mapped_file_.MapExisting`/`Open`) with no separate bounds guard around this specific read: [5](#0-4) 

The resulting bogus 64-bit size value then flows into `TransientBytes::DecompressEntryContents`/`ReadEntryContents` and `OutputJar::AddJar`'s copy/inflate logic, which use it directly to drive `Inflate`/`WriteBytes` byte counts: [6](#0-5) 

This is the same bug-class pattern as CVE-2018-25020: a length/offset value is derived from attacker-controlled metadata and consumed by a size-driven operation (inflate loop / jump) without validating that the metadata region referenced actually contains enough entries — an out-of-bounds read leading to memory-disclosure or crash, and (via the corrupted size feeding decompression length checks) potential corruption of subsequent output jar content.

### Impact Explanation
An out-of-bounds read of attacker-influenced adjacent memory that is then used as a "trusted" size for a subsequent copy/decompress operation. Depending on layout this can cause a worker crash (denial of the build) or leak bytes from adjacent jar content / heap memory into the resulting size computation, and in the worst case drive a mismatched `WriteBytes`/`Inflate` length that corrupts the output artifact. This is reachable purely from bytes in a jar/zip that an unprivileged party can supply as a build dependency.

### Likelihood Explanation
`singlejar` runs on every Java build that merges jars, and jar entries with hand-crafted Zip64 extra fields are trivial to construct with any hex editor or zip-writing library — no special access or credentials are required, only that the crafted jar be consumed as a build input (e.g., a dependency archive whose sha256 pins the archive itself, but not necessarily the fine-grained internal per-entry Zip64 layout of that archive, so a legitimately-checksummed-but-adversarial jar upstream can still smuggle this malformed entry).

### Recommendation
In `Zip64ExtraField::attr64`, bounds-check `index` against `attr_count()` and return a defined error/sentinel (or make the caller detect failure) instead of performing the raw array read. In `CDH::compressed_file_size()`/`uncompressed_file_size()` and the equivalent `LH` methods, validate that the extra field's `attr_count()` is sufficient for the requested index (i.e., that the number of Zip64 attributes actually present matches what the 32-bit fields imply is present) before dereferencing, and reject the entry with a diagnostic if not.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` style proof: build a minimal `.jar` containing one entry whose Central Directory Header sets `compressed_file_size32 = 0xFFFFFFFF` and `uncompressed_file_size32 = 0xFFFFFFFF`, but whose accompanying `Zip64ExtraField` has `payload_size = 8` (i.e., `attr_count() == 1`, containing only the uncompressed size). Feed this jar as a `srcs`/dependency jar to a `java_library` and run `bazel build`; `CDH::compressed_file_size()` will compute index `1` (since `uncompressed_file_size32() == 0xFFFFFFFF`) and call `attr64(1)`, reading 8 bytes past the single declared attribute — observable via ASan (`heap-buffer-overflow`/`SEGV`) instrumentation of `singlejar_test` or by asserting the resulting `compressed_file_size()` differs from the crafted expected value when the extra bytes are set to a known sentinel pattern.

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

**File:** src/tools/singlejar/zip_headers.h (L159-165)
```text
  // The value of i-th attribute
  uint64_t attr64(int index) const { return le64toh(attr_[index]); }
  void attr64(int index, uint64_t v) { attr_[index] = htole64(v); }

  // Attribute count
  int attr_count() const { return payload_size() / sizeof(attr_[0]); }
  void attr_count(int n) { payload_size(n * sizeof(attr_[0])); }
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

**File:** src/tools/singlejar/input_jar.cc (L23-52)
```text
bool InputJar::Open(const std::string& path) {
  if (!path_.empty()) {
    diag_errx(1, "%s:%d: This instance is already handling %s\n", __FILE__,
              __LINE__, path_.c_str());
  }
  if (!mapped_file_.Open(path)) {
    diag_warn("%s:%d: Cannot open input jar %s", __FILE__, __LINE__,
              path.c_str());
    mapped_file_.Close();
    return false;
  }
  if (mapped_file_.size() < sizeof(ECD)) {
    diag_warnx(
        "%s:%d: %s is only 0x%zx"
        " bytes long, should be at least 0x%zx bytes long",
        __FILE__, __LINE__, path.c_str(), mapped_file_.size(), sizeof(ECD));
    mapped_file_.Close();
    return false;
  }
  return LocateCentralDirectory(path);
}

bool InputJar::Open(const std::string& path, unsigned char* data,
                    size_t length) {
  if (path.empty()) {
    diag_errx(1, "%s:%d: A non-empty path is required\n", __FILE__, __LINE__);
  }
  mapped_file_.MapExisting(data, data + length);
  return LocateCentralDirectory(path);
}
```

**File:** src/tools/singlejar/transient_bytes.h (L77-102)
```text
  void ReadEntryContents(const CDH* cdh, const LH* lh) {
    uint64_t uncompressed_file_size;
    if (cdh->no_size_in_local_header()) {
      uncompressed_file_size = cdh->uncompressed_file_size();
    } else {
      uncompressed_file_size = lh->uncompressed_file_size();
    }
    Append(lh->data(), uncompressed_file_size);
  }

  // Appends the contents of the compressed Zip entry. Resets the inflater
  // used to decompress.
  void DecompressEntryContents(const CDH* cdh, const LH* lh,
                               Inflater* inflater) {
    uint64_t old_total_out = inflater->total_out();
    uint64_t in_bytes;
    uint64_t out_bytes;
    const uint8_t* data = lh->data();

    if (cdh->no_size_in_local_header()) {
      in_bytes = cdh->compressed_file_size();
      out_bytes = cdh->uncompressed_file_size();
    } else {
      in_bytes = lh->compressed_file_size();
      out_bytes = lh->uncompressed_file_size();
    }
```
