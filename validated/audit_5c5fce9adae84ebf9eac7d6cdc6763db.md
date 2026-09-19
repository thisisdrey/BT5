### Title
Out-of-bounds read from a malformed ZIP64 extra field is copied into build output - (File: src/tools/singlejar/zip_headers.h)

### Summary
`singlejar`, the C++ tool Bazel uses to merge JAR files (e.g. for `java_binary`/`java_library` deploy jars, which routinely include third-party dependency jars fetched via `http_jar`/`http_archive` or Maven), parses the ZIP64 extra field of each Local File Header without validating that the extra field actually contains as many 64-bit attributes as the code assumes. A hostile jar (self-consistent with its own pinned sha256/integrity hash, since the checksum only proves byte-for-byte identity, not structural validity) can declare a 32-bit `compressed_size` of `0xFFFFFFFF` while keeping `uncompressed_size` small, causing the parser to read an 8-byte attribute slot that was never validated to exist inside the Zip64 extra field payload.

### Finding Description
`LH::compressed_file_size()` and `LH::uncompressed_file_size()` unconditionally index into the `Zip64ExtraField` by fixed slot number (`attr64(1)` for compressed size, `attr64(0)` for uncompressed size) whenever the corresponding 32-bit field equals `0xFFFFFFFF`: [1](#0-0) 

Per the ZIP64 spec, the extra field only stores the subset of {uncompressed_size, compressed_size, offset} that actually overflowed 32 bits, always in that fixed order. So if only `compressed_size` overflows (attacker sets `uncompressed_file_size32 = 100`, `compressed_file_size32 = 0xFFFFFFFF`), the Zip64 extra field legitimately contains only **one** 8-byte attribute (holding the compressed size) at index 0 — yet the code reads index 1: [2](#0-1) 

`ExtraField::find` only validates that the field's declared `size()` (i.e. `sizeof(ExtraField) + payload_size()`) fits within the extra-fields buffer bounds — it never checks `attr_count()` against the number of attributes the caller is about to access: [3](#0-2) 

The resulting garbage 64-bit value becomes `compressed_file_size()`, which `OutputJar::AddJar` uses directly to compute how many bytes to copy from the memory-mapped input jar into the output jar: [4](#0-3) [5](#0-4) 

Because `num_bytes` is now attacker-influenced garbage (derived from adjacent, unvalidated memory), `WriteBytes(input_jar.mapped_start() + copy_from, num_bytes)` can read far past the bounds of the mapped input file and copy that out-of-bounds memory content straight into the merged output JAR.

### Impact Explanation
This breaks the invariant that "untrusted content stays data": bytes from a hostile, but hash-pinned, dependency jar cause the tool to read and exfiltrate adjacent process memory (heap allocations, other mmap'd input jars, etc.) into a build artifact that is then distributed/shipped as part of the built binary. This is a concrete cross-boundary memory disclosure into build output, not merely a crash, and (depending on `num_bytes` magnitude and mapping layout) can also crash the singlejar process.

### Likelihood Explanation
Any build that merges a maliciously-authored jar (self-consistent with its own hash, such as a jar whose URL/registry entry an attacker controls before the sha256 is pinned by the victim, or a legitimately-hashed but adversarially constructed artifact) into a `java_binary`/`java_library` triggers this path automatically via `singlejar`'s standard jar-merging logic — no special flags are required.

### Recommendation
In `Zip64ExtraField`, validate `attr_count()` against the index being requested before calling `attr64(index)` (and equivalently guard the CDH-side accessors), rejecting/erroring out entries whose Zip64 extra field does not contain enough attributes for the 32-bit fields that claim to need extension, and bound `WriteBytes`'s `num_bytes` against the true remaining size of the mapped input jar.

### Proof of Concept
Construct a ZIP `LH` where `uncompressed_file_size32 = 100` (no ext64 needed) and `compressed_file_size32 = 0xFFFFFFFF` (ext64 needed), attach a Zip64 extra field with `payload_size = 8` (a single 8-byte attribute, per spec representing the compressed size at index 0), and feed this jar to `singlejar --output out.jar --sources evil.jar`. `LH::compressed_file_size()` will call `z64->attr64(1)`, reading 8 bytes past the validated Zip64 extra field payload; `OutputJar::AddJar` will then use this garbage value as `num_bytes` for `WriteBytes(input_jar.mapped_start() + copy_from, num_bytes)`, copying out-of-bounds memory into `out.jar`. This can be encoded as a `BuildIntegrationTestCase`/`src/test/shell/bazel` scenario that runs `singlejar` on a crafted jar (analogous to the existing malformed-extra-field test at `src/tools/singlejar/output_jar_simple_test.cc:1179-1225`) and asserts the resulting output jar does not contain data beyond the declared entry bounds.

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

**File:** src/tools/singlejar/zip_headers.h (L149-176)
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

**File:** src/tools/singlejar/output_jar.cc (L666-671)
```text
    // Do the actual copy.
    if (!WriteBytes(input_jar.mapped_start() + copy_from, num_bytes)) {
      diag_err(1, "%s:%d: Cannot write %zu bytes of %.*s from %s", __FILE__,
               __LINE__, num_bytes, file_name_length, file_name,
               input_jar_path.c_str());
    }
```
