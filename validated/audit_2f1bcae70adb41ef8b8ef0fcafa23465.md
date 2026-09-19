### Title
Heap over-read when merging jar entry contents via attacker-controlled Local File Header size fields - (File: src/tools/singlejar/transient_bytes.h)

### Summary
`singlejar` merges certain resource entries (e.g. `META-INF/services/*`, `META-INF/spring.handlers`, log4j2 plugin cache files) across all input jars given via `--sources`, using `TransientBytes::ReadEntryContents` / `TransientBytes::DecompressEntryContents`. These methods trust size fields taken straight from the mmap'd Local File Header (`LH`) of an attacker-supplied jar without verifying that the declared size actually fits within the bytes remaining in the memory-mapped input file, unlike the equivalent code path in `third_party/ijar/zip.cc`, which explicitly calls `EnsureRemaining()` before trusting any length field taken from zip structures.

### Finding Description
`TransientBytes::ReadEntryContents` and `DecompressEntryContents` in [1](#0-0)  read `uncompressed_file_size`/`compressed_file_size` directly from the `CDH`/`LH` structures and then call `Append(lh->data(), uncompressed_file_size)` or feed `in_bytes`/`out_bytes` straight into `inflater->DataToInflate(data, in_bytes_chunk)`. Neither path checks that `lh->data() + uncompressed_file_size` (or `compressed_file_size`) stays within the bounds of the memory-mapped jar (`mapped_file_`) before reading.

The `LH` (Local File Header) fields such as `compressed_file_size32_`/`uncompressed_file_size32_` are attacker-controlled: they come directly from a jar file supplied as a build input (e.g. a third-party dependency jar downloaded and consumed by `singlejar` when producing a deploy jar), as seen in [2](#0-1) . If `cdh->no_size_in_local_header()` is false, the code path in `ReadEntryContents`/`DecompressEntryContents` uses the LH's own (attacker-set) size fields rather than the (also attacker-controlled but at least CDH-consistent) values, and no bound is enforced against the actual mmap extent (`mapped_file_.end()`), which is captured in `MappedFile::mapped()`/`address()` in [3](#0-2) .

By contrast, the sibling implementation in `third_party/ijar/zip.cc` explicitly validates every size field it reads from the zip file against remaining bytes before use, via `EnsureRemaining()`, e.g. before trusting `file_name_length_`/`extra_field_length_`/`compressed_size_` in `InputZipFile::ProcessLocalFileEntry` [4](#0-3) . `TransientBytes` has no analogous check, so a crafted jar with an inflated `uncompressed_file_size`/`compressed_file_size` field in its Local File Header causes `Append`/`Inflate` to read heap memory beyond the mapped input file into the merged buffer that becomes part of the output jar.

This affects `Concatenator::Merge` [5](#0-4)  (used for spring.handlers/spring.schemas/services merging and classpath resources) and `Log4J2PluginDatCombiner::Merge` [6](#0-5) , both of which call `ReadEntryContents`/`DecompressEntryContents` with no additional bounds validation of their own.

### Impact Explanation
An attacker who can supply a jar file that becomes an input to `singlejar` (e.g. a third-party jar dependency fetched from a registry/mirror and merged into a `java_binary`'s deploy jar) can craft a Local File Header whose size fields exceed the actual amount of file data present. When that entry is routed through a merging combiner (services files, spring handler/schema files, log4j2 plugin cache files, or classpath resources), `singlejar` reads past the end of the mmap'd input file into adjacent heap/process memory and writes that leaked memory into the output jar — an information disclosure analogous to the reported SQLite `zipfileInflate` heap over-read via a crafted ZIP.

### Likelihood Explanation
This requires the attacker's jar entry to be routed through one of the "merge" combiners rather than the default raw-copy path, and requires the LH size field to be inconsistent with the CDH in the specific "no_size_in_local_header" false branch. It is unclear from the available code whether `no_size_in_local_header()` (definition not found in indexed content) already forces the CDH-derived size to be used in the common (correctly-declared) case, which would materially reduce reachability. This uncertainty means the exact trigger conditions (when the LH-declared size, rather than the CDH size, is actually used) could not be fully confirmed with the available tools.

### Recommendation
In `TransientBytes::ReadEntryContents` and `DecompressEntryContents` (`src/tools/singlejar/transient_bytes.h`), validate that `lh->data() + in_bytes`/`out_bytes` does not exceed the mapped input file's end before calling `Append`/`inflater->DataToInflate`, mirroring the `EnsureRemaining()` bounds checks already present in `third_party/ijar/zip.cc`. Reject or truncate entries whose declared sizes exceed the bytes actually available in the mapped input jar.

### Proof of Concept
A concrete JUnit/shell-test PoC could not be fully constructed from available context because the exact behavior of `CDH::no_size_in_local_header()` (which selects between LH-declared and CDH-declared sizes) could not be located in the indexed code. A reproduction would need to:
1. Build a jar with one entry named `META-INF/services/foo` (or another combiner-routed resource) whose CDH indicates the LH does carry explicit sizes.
2. Set the LH's `compressed_file_size32_`/`uncompressed_file_size32_` fields far larger than the actual number of file-data bytes following the header, while leaving the CDH's own recorded sizes small/consistent.
3. Run `singlejar --sources crafted.jar --output out.jar` and observe (e.g., via ASan) a heap-buffer-overflow read in `TransientBytes::ReadEntryContents`/`DecompressEntryContents`, and/or inspect `out.jar`'s merged resource for leaked heap bytes beyond the legitimate file content. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** src/tools/singlejar/zip_headers.h (L240-268)
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

**File:** src/tools/singlejar/mapped_file.h (L47-58)
```text
  bool mapped(const void* addr) const {
    return mapped_start_ <= addr && addr < mapped_end_;
  }

  const unsigned char* start() const { return mapped_start_; }
  const unsigned char* end() const { return mapped_end_; }
  const unsigned char* address(int64_t offset) const {
    return mapped_start_ + offset;
  }
  int64_t offset(const void* address) const {
    return reinterpret_cast<const unsigned char*>(address) - mapped_start_;
  }
```

**File:** third_party/ijar/zip.cc (L332-371)
```text
int InputZipFile::ProcessLocalFileEntry(
    size_t compressed_size, size_t uncompressed_size) {
  if (EnsureRemaining(26, "extract_version") < 0) {
    return -1;
  }
  extract_version_ = get_u2le(p);
  general_purpose_bit_flag_ = get_u2le(p);

  if ((general_purpose_bit_flag_ & ~GENERAL_PURPOSE_BIT_FLAG_SUPPORTED) != 0) {
    return error("Unsupported value (0x%04x) in general purpose bit flag.\n",
                 general_purpose_bit_flag_);
  }

  compression_method_ = get_u2le(p);

  if (compression_method_ != COMPRESSION_METHOD_DEFLATED &&
      compression_method_ != COMPRESSION_METHOD_STORED) {
    return error("Unsupported compression method (%d).\n",
                 compression_method_);
  }

  // skip over: last_mod_file_time, last_mod_file_date, crc32
  p += 2 + 2 + 4;
  compressed_size_ = get_u4le(p);
  uncompressed_size_ = get_u4le(p);
  file_name_length_ = get_u2le(p);
  extra_field_length_ = get_u2le(p);

  if (EnsureRemaining(file_name_length_, "file_name") < 0) {
    return -1;
  }
  file_name_ = p;
  p += file_name_length_;

  if (EnsureRemaining(extra_field_length_, "extra_field") < 0) {
    return -1;
  }
  extra_field_ = p;
  p += extra_field_length_;

```

**File:** src/tools/singlejar/combiners.cc (L40-57)
```text
bool Concatenator::Merge(const CDH* cdh, const LH* lh) {
  if (insert_newlines_ && buffer_.get() && buffer_->data_size() &&
      '\n' != buffer_->last_byte()) {
    Append("\n", 1);
  }
  CreateBuffer();
  if (Z_NO_COMPRESSION == lh->compression_method()) {
    buffer_->ReadEntryContents(cdh, lh);
  } else if (Z_DEFLATED == lh->compression_method()) {
    if (!inflater_) {
      inflater_.reset(new Inflater());
    }
    buffer_->DecompressEntryContents(cdh, lh, inflater_.get());
  } else {
    diag_errx(2, "%s is neither stored nor deflated", filename_.c_str());
  }
  return true;
}
```

**File:** src/tools/singlejar/log4j2_plugin_dat_combiner.cc (L165-177)
```text
bool Log4J2PluginDatCombiner::Merge(const CDH* cdh, const LH* lh) {
  TransientBytes bytes_;
  if (lh->compression_method() == Z_NO_COMPRESSION) {
    bytes_.ReadEntryContents(cdh, lh);
  } else if (lh->compression_method() == Z_DEFLATED) {
    if (!inflater_) {
      inflater_.reset(new Inflater());
    }
    bytes_.DecompressEntryContents(cdh, lh, inflater_.get());
  } else {
    diag_errx(2, "neither stored nor deflated");
  }

```
