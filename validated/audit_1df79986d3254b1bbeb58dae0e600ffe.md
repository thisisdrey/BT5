### Title
Heap buffer overflow in `OutputJar::WriteEntry` from mismatched Zip64 extra-field length vs. `attr_count()`-derived size - (File: `src/tools/singlejar/output_jar.cc`)

### Summary
`OutputJar::WriteEntry` allocates the Central Directory Header (CDH) buffer using a size derived from `Zip64ExtraField::space_needed(lh_zip64_ef->attr_count())`, but the copy loop that fills that buffer skips bytes based on the *on-disk* `ExtraField::size()` (derived from the raw `payload_size` field). These two derivations of "how many bytes the Local Header's Zip64 extra field occupies" are not required to agree, and an attacker who controls the input jar/zip content (e.g., fetched via `http_jar`/`http_archive` and merged by `singlejar`, an internal Bazel action) can make them diverge, causing the loop that copies remaining (non-zip64) extra fields to overrun the too-small CDH allocation.

### Finding Description
In `OutputJar::WriteEntry`: [1](#0-0) 

`lh_zip64_size` is computed as `Zip64ExtraField::space_needed(lh_zip64_ef->attr_count())`, where `attr_count()` is `payload_size() / sizeof(uint64_t)`: [2](#0-1) 

The CDH buffer is then allocated as `sizeof(CDH) + entry->file_name_length() + entry->extra_fields_length() + zip64_size - lh_zip64_size`, i.e. it assumes the on-disk Zip64 field occupies exactly `lh_zip64_size` bytes.

The subsequent copy loop, however, walks the local header's raw extra fields and advances/skips using each field's own `size()` (`sizeof(ExtraField) + payload_size()`), not the `attr_count()`-derived size: [3](#0-2) 

Both `payload_size()` (hence `attr_count()`) and `size()` are read from the same 2-byte field in `ExtraField` (`payload_size_`), so in the "happy path" they agree. However, `attr_count()` truncates via integer division (`payload_size() / 8`), so a crafted Zip64 extra field whose `payload_size` is not a multiple of 8 (e.g., `payload_size = 9`) yields `attr_count() = 1` → `lh_zip64_size = space_needed(1) = 12` bytes, while the actual on-disk field occupies `sizeof(ExtraField) + 9 = 13` bytes and the loop's `ef->size()` correctly returns 13. The one-byte discrepancy propagates: the CDH buffer is allocated one byte smaller than what the copy loop will end up writing when other, subsequent extra fields are copied into `cdh_extra_fields`, since the buffer capacity was computed assuming 12 bytes were consumed by the (skipped) zip64 field but the loop actually consumes 13 bytes before writing the following fields — causing the `memcpy(cdh_extra_fields, ef, ef->size())` for a later field to write past the end of the allocated CDH region. Since additional extra fields (e.g. `UnixTimeExtraField`) can be placed after the malformed Zip64 field, the attacker has a way to line up subsequent field content precisely at the overflow point, similarly to how `MqttDecode_Publish` in CVE-2021-45932 read a length field that didn't match the actual consumed byte count, causing a small heap overflow.

The malformed-extra-field bounds check in the loop (`ziph::byte_ptr(ef) + ef->size() > ziph::byte_ptr(lh_ef_end)`) only validates that the field doesn't run past the end of the *source* extra-fields blob; it never validates the assumption baked into the *destination* buffer's size (`lh_zip64_size`), so it does not catch this class of bug. [4](#0-3) 

This code path is reached from `AddJar`, which iterates entries of an input jar opened via `InputJar::Open`/`NextEntry` — an entirely attacker-controlled memory-mapped file (no parsing invariant beyond signature checks) — feeding directly into `WriteEntry`. [5](#0-4) 

### Impact Explanation
`singlejar` is a native (C++) helper binary invoked as part of ordinary Bazel build actions (merging/deploying jars). A crafted, attacker-supplied `.jar`/`.zip` input (e.g. fetched through `http_jar`/`http_archive` with a *correct* SHA-256 that the attacker chooses, since the attacker controls the origin content) can trigger a small heap-based buffer overflow during the build, potentially corrupting heap metadata or adjacent allocations in the `singlejar` process — this is a memory-safety violation in native code executed as part of the build, analogous in kind (small, length-mismatch-driven heap overflow in a length-prefixed field decoder) to CVE-2021-45932. Exact severity (crash vs. exploitable corruption) depends on heap allocator behavior and is not established here; further fuzzing/exploitation work would be needed to determine impact beyond a heap overflow write.

### Likelihood Explanation
The attacker model fits: the input jar is untrusted content the build merely trusts to match a pinned digest, not to be well-formed; the checksum only proves byte-for-byte identity with what the attacker published, not internal consistency of the Zip64 extra field. Triggering requires crafting a Local Header extra-field chain with a Zip64 field whose `payload_size` is not a multiple of 8, followed by additional extra field bytes to be copied — a well-defined, reachable byte-level manipulation of an ordinary zip/jar file, no privileged access needed.

### Recommendation
In `WriteEntry`, compute `lh_zip64_size` using the on-disk `lh_zip64_ef->size()` (as used by the copy loop) rather than `Zip64ExtraField::space_needed(lh_zip64_ef->attr_count())`, or explicitly validate `payload_size() % sizeof(uint64_t) == 0` for Zip64 extra fields (and reject malformed entries) before using `attr_count()` for allocation sizing. Additionally, add a runtime assertion/bounds check in the copy loop that memcpy destination writes never exceed the CDH allocation size, independent of the length fields trusted from the input.

### Proof of Concept
A `BuildIntegrationTestCase`/`zip_headers_test.cc`-style unit test can reproduce this deterministically without needing Bazel's full build machinery:
1. Construct a Local Header (`LH`) buffer with `extra_fields_length` covering: (a) a `Zip64ExtraField` with `payload_size = 9` (not a multiple of 8) — i.e., `tag=1, payload_size=9`, followed by 9 arbitrary payload bytes — and (b) a subsequent generic `ExtraField` (or `UnixTimeExtraField`) with a payload large enough that when copied after the (mis-sized) zip64 skip, it writes past the CDH buffer boundary computed from `space_needed(attr_count())`.
2. Call `OutputJar::WriteEntry` (or a smaller unit test directly exercising the `lh_zip64_size`/copy-loop logic extracted into a testable helper) with this crafted `LH*` and observe (with ASan/heap-checker enabled) a heap-buffer-overflow write during the `memcpy(cdh_extra_fields, ef, ef->size())` call.
3. Expected: AddressSanitizer reports a heap-buffer-overflow WRITE inside `OutputJar::WriteEntry` at the `memcpy` in `output_jar.cc` line ~787, confirming the CDH allocation was undersized relative to the bytes actually written.

Note: I was not able to fully trace whether `singlejar`'s callers always run with ASan/hardened allocators in CI, nor confirm the exact byte alignment needed to guarantee an out-of-bounds write versus merely a benign slack-byte consumption in every allocator; a background Devin session with build/test tooling would be needed to actually compile and run this PoC to confirm exploitability end-to-end.

### Citations

**File:** src/tools/singlejar/output_jar.cc (L583-588)
```text
        Concatenator combiner(jar_entry->file_name_string());
        if (!combiner.Merge(jar_entry, lh)) {
          diag_err(1, "%s:%d: cannot add %.*s", __FILE__, __LINE__,
                   jar_entry->file_name_length(), jar_entry->file_name());
        }
        WriteEntry(combiner.OutputEntry(output_compressed));
```

**File:** src/tools/singlejar/output_jar.cc (L749-756)
```text
  const Zip64ExtraField* lh_zip64_ef = entry->zip64_extra_field();
  uint16_t lh_zip64_size =
      lh_zip64_ef == nullptr
          ? 0
          : Zip64ExtraField::space_needed(lh_zip64_ef->attr_count());
  CDH* cdh = reinterpret_cast<CDH*>(
      ReserveCdh(sizeof(CDH) + entry->file_name_length() +
                 entry->extra_fields_length() + zip64_size - lh_zip64_size));
```

**File:** src/tools/singlejar/output_jar.cc (L774-793)
```text
  auto lh_ef_begin = reinterpret_cast<const ExtraField*>(entry->extra_fields());
  auto lh_ef_end = reinterpret_cast<const ExtraField*>(
      ziph::byte_ptr(lh_ef_begin) + entry->extra_fields_length());
  ExtraField* cdh_extra_fields =
      reinterpret_cast<ExtraField*>(const_cast<uint8_t*>(cdh->extra_fields()));
  uint16_t out_ef_length = 0;
  for (const ExtraField* ef = lh_ef_begin; ef < lh_ef_end; ef = ef->next()) {
    if (ziph::byte_ptr(ef) + sizeof(ExtraField) > ziph::byte_ptr(lh_ef_end) ||
        ziph::byte_ptr(ef) + ef->size() > ziph::byte_ptr(lh_ef_end)) {
      diag_errx(1, "malformed extra field in LH for %.*s",
                (int)entry->file_name_length(), entry->file_name());
    }
    if (!ef->is_zip64()) {
      memcpy(cdh_extra_fields, ef, ef->size());
      cdh_extra_fields = reinterpret_cast<ExtraField*>(
          reinterpret_cast<uint8_t*>(cdh_extra_fields) + ef->size());
      out_ef_length += ef->size();
    }
  }
  cdh->extra_fields(cdh->extra_fields(), out_ef_length);
```

**File:** src/tools/singlejar/zip_headers.h (L163-171)
```text
  // Attribute count
  int attr_count() const { return payload_size() / sizeof(attr_[0]); }
  void attr_count(int n) { payload_size(n * sizeof(attr_[0])); }

  // Space needed for this field to accommodate n_attr attributes
  static uint16_t space_needed(int n_attrs) {
    return n_attrs > 0 ? sizeof(Zip64ExtraField) + n_attrs * sizeof(uint64_t)
                       : 0;
  }
```
