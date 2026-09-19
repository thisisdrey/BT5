### Title
Out-of-bounds read via unvalidated Zip64 extra-field attribute index in singlejar's `Zip64ExtraField::attr64` - (File: `src/tools/singlejar/zip_headers.h`)

### Summary
`Zip64ExtraField::attr64(int index)` indexes into a variable-length `attr_[]` array using an index derived from which 32-bit size/offset fields are flagged as "overflowed" (`0xFFFFFFFF`), without ever checking that `attr_count()` (derived from the field's declared `payload_size()`) actually covers that index.

### Finding Description
`LH::compressed_file_size()`, `LH::uncompressed_file_size()`, and `CDH::local_header_offset()` each compute an `attr_no`/index (0, 1, or 2) based on which of the 32-bit `compressed_file_size32`/`uncompressed_file_size32`/`local_header_offset32` fields equal `0xFFFFFFFF` (i.e., "value stored in Zip64 extra field"), per APPNOTE 4.5.3's ordering rule (uncompressed, then compressed, then offset — only the overflowed ones are present, in that order). [1](#0-0) [2](#0-1) 

The computed index is then passed straight to `Zip64ExtraField::attr64(index)`, which reads `attr_[index]` with no bounds check against `attr_count()` (which is simply `payload_size() / 8`): [3](#0-2) 

`Zip64ExtraField::find()` (built on `ExtraField::find`) only validates that the *field header itself* (4 bytes) plus its *declared* `payload_size()` fit within the extra-fields buffer — it never checks that the payload actually contains enough 8-byte attributes for the specific 32-bit fields that are flagged as overflowed: [4](#0-3) 

Consequently, an attacker who supplies a `.zip`/`.jar` file (e.g. a maven/http-archive dependency, or a source jar consumed by `singlejar` when building a deploy jar) can craft a Local Header or Central Directory Header where:
- one or more of `compressed_file_size32`/`uncompressed_file_size32`/`local_header_offset32` are set to `0xFFFFFFFF` (signaling "stored in Zip64 extra field"), but
- the accompanying Zip64 extra field's `payload_size` is deliberately short — e.g. it declares only 1 attribute (8 bytes) while the flags on the 32-bit fields require reading `attr_[1]` or `attr_[2]`.

Because `attr_count()` is never consulted before `attr64(index)` dereferences `attr_[index]`, this reads memory past the *declared* end of the Zip64 payload — and potentially past the actual extra-fields buffer / the entire memory-mapped input file (`InputJar` maps the archive read-only via `MappedFile`), directly analogous to `vgacon_scrolldelta`'s unchecked scrollback offset causing an out-of-bounds read of kernel memory (CID-973c096f6a85). The read result (a bogus 64-bit size/offset) is then used downstream, e.g. as `copy_from`/`num_bytes` for further memory-mapped-pointer arithmetic in `OutputJar::AddJar`: [5](#0-4) 
or as the size fed to `TransientBytes::ReadEntryContents`/`DecompressEntryContents`, which directly appends `lh->data()` for `uncompressed_file_size` bytes: [6](#0-5) 

This differs from the already-tested `output_jar_simple_test.cc` malformed-extra-field case, which only exercises `ExtraField::find`'s own header/size bound check (guarded, causes a `diag_errx`): [7](#0-6) 
That existing check does **not** protect against the `attr64(index)` OOB, because the Zip64 field itself can be perfectly well-formed (fits within extra fields, correct header) while still being *too short* for the specific `attr_no` computed from the 32-bit field flags.

### Impact Explanation
An attacker who controls the bytes of an archive/jar consumed by Bazel's `singlejar` tool (used to build deploy jars, e.g. from `java_binary`, `java_import`, fetched third-party jars via `http_archive`/Maven rules) can trigger an out-of-bounds read of process memory adjacent to (or beyond) the memory-mapped input file. The leaked bytes are interpreted as a 64-bit size/offset and used in subsequent copy/read operations, which can manifest as further OOB reads of size determined by attacker-controlled garbage, or a crash (segfault) reading beyond the mmap'd region — a concrete "read outside the repository/exec-root" style memory-safety violation of the class targeted by this scan.

### Likelihood Explanation
Reaching this code only requires supplying a syntactically valid but maliciously-crafted zip/jar with a Zip64 extra field shorter than what the size-field flags imply — a simple, deterministic byte-level modification requiring no privileged access, matching the "unprivileged attacker publishing content a victim's build consumes" threat model. Because `singlejar` is invoked automatically when building any `java_binary`/`java_import`-style target that includes a third-party or CI-fetched jar, the surface is broadly reachable in ordinary Bazel Java builds.

### Recommendation
In `Zip64ExtraField::attr64()` (or, better, at each call site in `LH`/`CDH`), validate `index < attr_count()` before dereferencing `attr_[index]`, and treat a too-short Zip64 extra field as a fatal malformed-archive error (as is already done for other extra-field bound violations, e.g. in `OutputJar::WriteEntry`/`AppendToDirectoryBuffer`). Apply the same validation uniformly to all three call sites: `LH::compressed_file_size()`, `LH::uncompressed_file_size()`, and `CDH::local_header_offset()`.

### Proof of Concept
Construct a minimal jar/zip (analogous to `CreateZipWithMalformedExtraField` in `output_jar_simple_test.cc`) where:
1. The Local Header sets `uncompressed_file_size32 = 0xFFFFFFFF` and `compressed_file_size32 = 0xFFFFFFFF` (both flagged as 64-bit, requiring `attr_[0]` and `attr_[1]`).
2. The Zip64 extra field attached to that Local Header has `payload_size = 8` (i.e., `attr_count() == 1`), so only `attr_[0]` is backed by real payload bytes; `attr_[1]` lies past the field's own buffer.
3. Feed this file through `InputJar`/`OutputJar::AddJar` (as exercised by `TEST_F(OutputJarSimpleTest, ...)` harness) and observe that `lh->compressed_file_size()` (which calls `z64->attr64(1)`) reads 8 bytes beyond the declared Zip64 payload — verifiable with ASan/valgrind showing a heap-buffer-overflow / read past the mmap'd extent when the crafted entry is placed at the end of the mapped file.

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

**File:** src/tools/singlejar/transient_bytes.h (L76-102)
```text
  // Appends the contents of the uncompressed Zip entry.
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
