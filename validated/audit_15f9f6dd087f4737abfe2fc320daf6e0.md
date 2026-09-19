## Title
Out-of-bounds read via unchecked `Zip64ExtraField::attr64()` index in singlejar's zip header parsing - (File: `src/tools/singlejar/zip_headers.h`)

### Summary
`Zip64ExtraField::attr64(int index)` performs a raw array access `attr_[index]` with no bounds check against the field's own declared payload size, and every caller (`LH::compressed_file_size()`, `LH::uncompressed_file_size()`, `CDH::compressed_file_size()`, `CDH::uncompressed_file_size()`, `CDH::local_header_offset()`) derives `index` from other attacker-controlled 32-bit size fields rather than from the actual number of 8-byte attributes present in the extra field. [1](#0-0)  This is structurally the same bug class as CVE-2026-80598 (`decompress_lznt` OOB table read): a table/array is indexed by a value computed from untrusted input without validating it against the array's true bounds.

### Finding Description
`Zip64ExtraField` models the ZIP64 "extra field" payload as a flexible array of `uint64_t` values (`attr_[]`), with `attr_count()` correctly computed as `payload_size() / sizeof(uint64_t)`. [2](#0-1)  However, `attr64(int index)` never checks `index < attr_count()`:

```cpp
uint64_t attr64(int index) const { return le64toh(attr_[index]); }
``` [3](#0-2) 

The callers compute `index` from the *other* 32-bit fields' "is this 0xFFFFFFFF" sentinel bits, not from how many attributes the Zip64 extra field itself actually contains:

- `LH::compressed_file_size()` / `CDH::compressed_file_size()` call `z64->attr64(1)` or `z64->attr64(ziph::zfield_has_ext64(uncompressed_file_size32()))` unconditionally once the 32-bit field equals `0xFFFFFFFF`. [4](#0-3) [5](#0-4) 
- `CDH::local_header_offset()` computes `attr_no` by summing two independent sentinel checks and calls `z64->attr64(attr_no)` without ever checking `attr_no < z64->attr_count()`. [6](#0-5) 

A malicious archive can set, e.g., `uncompressed_file_size32 = 0xFFFFFFFF` and `local_header_offset32 = 0xFFFFFFFF` while supplying a Zip64 extra field whose `payload_size` only holds 0 or 1 attribute (`attr_count() == 0` or `1`). This forces `attr_no` to resolve to index `1` or `2` even though only 0 or 1 `uint64_t` slots exist in the field, causing `attr64()` to read memory past the end of the extra-field payload (and potentially past the end of the mapped/allocated central-directory buffer).

Unlike `ExtraField::find()`, which does bounds-check that a discovered extra field's `tag + size` fits within `[start, end)` before returning a pointer to it (as exercised by the existing `MalformedExtraField` death test) [7](#0-6) [8](#0-7) , no equivalent check exists once a valid, well-formed-but-undersized `Zip64ExtraField` (tag `0x0001`) has been located — the OOB happens *inside* that already-validated field, one level deeper, exactly analogous to the LZNT table-index bug (a validated container, unvalidated internal index).

This code is reached whenever `singlejar` (Bazel's jar-merging tool used to build `deploy_jar`/`_deploy.jar` and combined jars) parses the central directory and local headers of an input JAR/ZIP — i.e., any dependency jar an unprivileged party can publish (e.g., via `http_jar`, `http_archive`, or a Maven/HTTP dependency) that ends up as a `--sources` input to singlejar.

### Impact Explanation
An attacker who can get a victim's build to consume a crafted JAR/ZIP (a hostile HTTP/registry artifact, no MITM or local access needed) can trigger an out-of-bounds heap read inside the Bazel build tool (`singlejar`) while it computes sizes/offsets from the central directory. Depending on layout this can cause a crash (denial of service for the build) or, since the read value directly becomes `compressed_file_size()`/`uncompressed_file_size()`/`local_header_offset()` used later to memcpy/advance pointers over entry data, it can potentially be leveraged into further out-of-bounds reads/copies when the (garbage) size/offset is subsequently used to index into the mapped archive buffer.

### Likelihood Explanation
Requires only a crafted ZIP64 extra field with an inconsistent/undersized payload relative to the sentinel `0xFFFFFFFF` markers in the 32-bit size/offset fields — a purely data-level, easily-constructed malformation, requiring no cryptographic bypass and no privileged access. Reachable via any singlejar invocation over untrusted jars (typical for external Java dependencies fetched by `http_jar`/`http_archive`), making the likelihood moderate-to-high whenever the build consumes third-party jars processed by singlejar.

### Recommendation
Add bounds checks in `Zip64ExtraField` (or its callers) so that `attr64(index)` — or the callers computing `index` — validate `index < attr_count()` before dereferencing, mirroring the existing `ExtraField::find()` containment check. Return a well-defined error/`0xFFFFFFFFFFFFFFFF` sentinel (consistent with the existing `z64 == nullptr` fallback) instead of performing the unchecked array access when the field doesn't contain enough attributes for the requested index.

### Proof of Concept
Extend the existing `output_jar_simple_test.cc` malformed-archive test pattern (`CreateZipWithMalformedExtraField`) to build a JAR whose Central Directory Header has `uncompressed_file_size32 = 0xFFFFFFFF` and `local_header_offset32 = 0xFFFFFFFF` but whose ZIP64 extra field (tag `0x0001`) has `payload_size = 8` (i.e., `attr_count() == 1`, holding only `uncompressed_file_size`). Feed this into `OutputJar::Doit()` via `--sources` as in `TEST_F(OutputJarSimpleTest, MalformedExtraField)` [8](#0-7) ; the code path invoking `CDH::local_header_offset()` will call `z64->attr64(1)` on a field that only has one valid attribute (index 0), reading 8 bytes past the intended payload — under ASan this manifests as a heap-buffer-overflow read, verifiable with a `JUnit`/`ASSERT_DEATH`-style test analogous to the current `MalformedExtraField` test.

**Note on confidence:** I was not able to fully trace `input_jar.cc`/`input_jar.h`'s exact call sequence into `output_jar.cc` within the available iterations to confirm the precise runtime code path and buffer layout (e.g., whether the mmap'd region always has enough trailing bytes to avoid a hard segfault vs. silently reading adjacent header bytes), so the "crash vs. silent misuse of garbage value" distinction should be verified with an actual ASan-instrumented reproduction before relying on this as a fully proven finding.

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
