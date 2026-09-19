### Title
Unvalidated `CDH::local_header_offset()` in `InputJar::LocalHeader` enables heap buffer overread on crafted JAR/ZIP central directory entries - (File: `src/tools/singlejar/input_jar.h`)

### Summary
`InputJar::LocalHeader()` computes a `Local Header` (`LH`) pointer directly from an attacker-controlled `local_header_offset` field stored in the Central Directory Header (`CDH`) of a JAR/ZIP file, without validating that the resulting pointer lies within the memory-mapped file bounds — unlike the adjacent `NextEntry()` logic, which explicitly performs this check for CDH traversal.

### Finding Description
`InputJar::NextEntry()` walks the Central Directory by computing the address of the next `CDH` record and explicitly validates it is within the mapped file before dereferencing it: [1](#0-0) 

However, `InputJar::LocalHeader()`, called immediately after from the same function, computes the `LH*` pointer using `cdh->local_header_offset() + preamble_size_` and hands it straight to `mapped_file_.address()` with **no equivalent bounds check**: [2](#0-1) 

`local_header_offset()` on `CDH` is parsed straight from untrusted archive bytes — either the raw 32-bit field, or (when the 32-bit field is `0xFFFFFFFF`) a 64-bit value pulled from an attacker-supplied Zip64 extra field inside the same crafted `CDH`: [3](#0-2) 

Because the returned `LH*` is not range-checked, any consumer that dereferences fields on it reads out-of-bounds heap memory. `OutputJar::AddJar()` is exactly such a consumer — for every jar entry it retrieves the `LH*` from `NextEntry()` and immediately reads `lh->size()`, `lh->compressed_file_size()`, and (if `no_size_in_local_header()` is set) walks further past `lh->data()` to a `DDR` record based on `jar_entry->compressed_file_size()`, all without any confirmation these reads stay inside the mapped input file: [4](#0-3) 

This mirrors the analog bug class: a crafted structured record (the wolfSSL PKCS7 `EnvelopedData`; here the ZIP `CDH`) contains an attacker-controlled offset/length that is used to index into a buffer without a bounds check, causing a heap overread when the derived pointer is dereferenced.

### Impact Explanation
`singlejar` (via `input_jar.cc`/`output_jar.cc`) is Bazel's Java rules toolchain used to merge/repackage JARs — including combining jars pulled in from `http_archive`/`http_jar` external dependencies and other build-time inputs whose bytes are attacker-influenced (e.g. a malicious `.jar` served from an untrusted mirror, or a crafted third-party JAR checked into an untrusted branch that CI builds). A crafted `CDH.local_header_offset` (or its Zip64 extra-field override) that points past the end of the mapped file, combined with `AddJar`'s subsequent unchecked reads of `LH` fields (`compressed_file_size`, `extra_fields_length`, `file_name_length`, `data()`), can cause reads beyond the memory-mapped input file — a heap buffer overread that can crash the build (info leak into copied output bytes / potential crash) during JAR merging.

### Likelihood Explanation
Likelihood is bounded by needing a build step that runs `singlejar`/`AddJar` over an attacker-influenced `.jar`/`.zip` file. This is realistic in Bazel builds that consume third-party Java dependencies fetched from an untrusted or compromised source (no sha256/integrity checked ZIP entry structure protects internal offsets — checksum verification on the whole archive at `http_archive` only protects against gross tampering post-download and does not validate internal ZIP structural invariants at the byte level the way an integrity digest would). The bug is triggerable purely by content, no privileged access needed.

### Recommendation
In `InputJar::LocalHeader()` (and any other place deriving a pointer from an untrusted offset field, e.g. `CDH::local_header_offset()`), validate the computed address with `mapped_file_.mapped(...)` (as already done in `NextEntry()`) before returning the `LH*`, and propagate a hard failure (`diag_errx`) analogous to the existing "Bad directory record" check if the offset is out of range. Additionally, `OutputJar::AddJar` should bound-check `lh->size()` / `lh->compressed_file_size()` derived reads against the mapped file end before use.

### Proof of Concept
A concrete PoC would require constructing a `.jar`/`.zip` file with a syntactically valid End-of-Central-Directory/CDH structure (passing `LocateCentralDirectory`'s checks in `src/tools/singlejar/input_jar.cc:94-165`) but with a `local_header_offset` (32-bit field, or via a crafted Zip64 extra field per `src/tools/singlejar/zip_headers.h:482-493`) that resolves to an address at or beyond the end of the mapped file, then running it through `OutputJar::AddJar` (e.g. via `bazel build` on a `java_binary`/`singlejar` merge action that includes this crafted jar). This would need to be implemented as a `JUnit`/`src/test/shell/bazel` test exercising `AddJar`/`InputJar::NextEntry` directly, which I was not able to fully construct or run in this analysis — verifying the exact overread (vs. a caught `mapped_file_` failure elsewhere, e.g. in `MappedFile::address()` itself) requires inspecting `src/tools/singlejar/mapped_file.h`'s `address()`/`mapped()` implementations, which I could not retrieve in this session (indexing limitation). I recommend a Devin session with full filesystem access to confirm whether `MappedFile::address()` performs any implicit clamping/bounds validation that would neutralize this specific path, and to build the concrete PoC archive.

### Citations

**File:** src/tools/singlejar/input_jar.h (L70-85)
```text
    const CDH* current_cdh = cdh_;
    const uint8_t* new_cdr = ziph::byte_ptr(cdh_) + cdh_->size();
    if (!mapped_file_.mapped(new_cdr)) {
      diag_errx(
          1,
          "Bad directory record at offset 0x%" PRIx64
          " of %s\n"
          "file name length = %u, extra_field length = %u, comment length = %u",
          CentralDirectoryRecordOffset(cdh_), path_.c_str(),
          cdh_->file_name_length(), cdh_->extra_fields_length(),
          cdh_->comment_length());
    }
    cdh_ = reinterpret_cast<const CDH*>(new_cdr);
    *local_header_ptr = LocalHeader(current_cdh);
    return current_cdh;
  }
```

**File:** src/tools/singlejar/input_jar.h (L94-97)
```text
  const LH* LocalHeader(const CDH* cdh) const {
    return reinterpret_cast<const LH*>(
        mapped_file_.address(cdh->local_header_offset() + preamble_size_));
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
