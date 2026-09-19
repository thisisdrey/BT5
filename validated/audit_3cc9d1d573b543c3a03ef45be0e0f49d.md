## Title
Out-of-bounds read parsing a corrupted `ijar` ZIP central directory during `ProcessCentralDirEntry` - (File: `third_party/ijar/zip.cc`)

### Summary
`ijar` (Bazel's stripped-jar generator, also reused by the `singlejar`/archive-processing tooling) parses a ZIP's central directory in `InputZipFile::ProcessCentralDirEntry`. Unlike `ProcessLocalFileEntry`, which calls `EnsureRemaining()` before every field read to check remaining buffer size against `input_file_->Length()`, `ProcessCentralDirEntry` performs no such bounds checks: it blindly advances a raw pointer `p` into the mmap'd input file based on attacker-controlled 16-bit length fields (`file_name_length`, `extra_field_length`, `file_comment_length`) and walks an "extra field" sub-loop using an attacker-controlled `data_size` with no check that `extra_p` stays within the mapped region.

### Finding Description
`FindZipCentralDirectory` only validates that `central_dir_offset + central_dir_size <= in_length` [1](#0-0) , i.e., it validates the aggregate central directory region reported by the End-Of-Central-Directory record, not each individual entry inside it. Once inside `ProcessCentralDirEntry`, the code reads fixed fields, then:

```
p += file_name_length;
const u1 *extra_p = p;
p += extra_field_length;
while (extra_p != p) {
  const u2 header_id = get_u2le(extra_p);
  const u2 data_size = get_u2le(extra_p);
  const u1 *extra = extra_p;
  extra_p += data_size;
  ...
}
p += file_comment_length;
``` [2](#0-1) 

None of `file_name_length`, `extra_field_length`, `file_comment_length`, or `data_size` are checked against the actual size of the mapped file or against the declared `central_dir_size` before being used to advance pointers and read further bytes (`get_u2le`, `get_u4le`, `get_u8le` for the zip64 extra field) via `memcpy`/direct dereference at `filename` copy and via `get_u8le(extra)` reads. A crafted archive with a central-directory entry whose `extra_field_length`/`data_size` exceeds the remaining mapped bytes causes the parser to read past the end of the `mmap`'d input buffer. By contrast, `InputZipFile::ProcessLocalFileEntry`, parsing the corresponding local file header, explicitly calls `EnsureRemaining()` before reading `file_name_length` and `extra_field_length` bytes [3](#0-2) , showing the project's own invariant ("do not read unchecked lengths from untrusted archives") is inconsistently applied and is specifically absent in the central-directory path.

This is directly analogous to CVE-2016-9799's `pklg_read_hci`/`btsnoop.c` bug class: a length field taken from an untrusted, corrupted binary record is used to index/advance into a buffer without validating it stays within the buffer bounds, causing an out-of-bounds read/crash when the tool processes the corrupted file.

### Impact Explanation
`ijar`/`zip.cc`'s `ZipExtractor` is exercised whenever Bazel or its tools process a ZIP-family archive coming from an untrusted origin - e.g. `http_archive`/`module_ctx.extract`/`repository_ctx.extract` acting on a `.jar`/`.zip`/`.aar`/`.war` fetched from a URL, or `singlejar`/`ijar` invoked on inputs from external repositories. A hostile origin server or a malicious file placed in a dependency archive can supply a corrupted/malformed central directory that passes the coarse aggregate-size check in `FindZipCentralDirectory` but has an individual entry whose extra-field length runs past the mapped file. This causes the tool (a C++ binary, so no JVM/managed bounds checking) to read out-of-bounds memory adjacent to the mmap region, producing a crash (SIGSEGV) or use of stray memory content in subsequent parsing decisions (e.g., feeding attacker-influenced or garbage bytes into zip64 size/offset determination, further affecting extraction).

### Likelihood Explanation
Reachability is straightforward: any archive (zip/jar/war/aar) fetched via `http_archive`/`download_and_extract`/`extract` from a URL controlled by an untrusted party is exactly the kind of content this parser processes, and the SHA256/integrity checksum on the archive as a whole does not protect against structural corruption designed to trigger this specific out-of-bounds access once the archive's overall bytes match the pinned hash (an attacker who controls the archive contents chooses both the hash and the malformed structure — the checksum is not a structural validator). No privileged access is required beyond serving/publishing the archive content.

### Recommendation
Add bounds validation in `ProcessCentralDirEntry` mirroring `EnsureRemaining()` used in `ProcessLocalFileEntry`: before advancing `p` by `file_name_length`, `extra_field_length`, or `file_comment_length`, and before reading `data_size` bytes in the extra-field loop, verify these values do not exceed the remaining bytes in `zipdata_in_`/`central_dir_`. Reject (return `false`/emit `error(...)`) and abort the build on malformed entries rather than continuing to dereference an unchecked pointer.

### Proof of Concept
A JUnit/`googletest`-based reproduction (analogous to the existing `MalformedExtraField` test in `singlejar`) would:
1. Build a minimal valid ZIP with one Central Directory Header entry whose `extra_field_length` (or a nested extra-field `data_size`) is set larger than the number of bytes actually present between the entry and the end of the mapped file/central directory.
2. Ensure the End-Of-Central-Directory `central_dir_size`/`central_dir_offset` fields remain internally consistent so `FindZipCentralDirectory`'s aggregate check passes.
3. Invoke `ZipExtractor::Create`/`ProcessAll` (as used by `ijar.cc`/`zip_main.cc`) on this archive and observe an out-of-bounds read/crash (or run under ASan to detect the heap-buffer-overflow/read) instead of a graceful `error(...)` rejection, in contrast to the graceful failure that `EnsureRemaining()` would have produced. This mirrors the existing `CreateZipWithMalformedExtraField`/`MalformedExtraField` pattern found in `src/tools/singlejar/output_jar_simple_test.cc` [4](#0-3) , but targeting `third_party/ijar/zip.cc`'s `ProcessCentralDirEntry`, which lacks the equivalent bounds check that `singlejar`'s `WriteEntry`/extra-field validation performs.

### Citations

**File:** third_party/ijar/zip.cc (L360-370)
```text
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

**File:** third_party/ijar/zip.cc (L507-543)
```text
  p += 16;  // skip to 'compressed size' field
  *compressed_size = get_u4le(p);
  *uncompressed_size = get_u4le(p);
  u2 file_name_length = get_u2le(p);
  u2 extra_field_length = get_u2le(p);
  u2 file_comment_length = get_u2le(p);
  p += 4;  // skip to external file attributes field
  *attr = get_u4le(p);
  *offset = get_u4le(p);
  {
    size_t len = (file_name_length < filename_size)
      ? file_name_length
      : (filename_size - 1);
    memcpy(reinterpret_cast<void*>(filename), p, len);
    filename[len] = 0;
  }
  p += file_name_length;
  const u1 *extra_p = p;
  p += extra_field_length;
  while (extra_p != p) {
    const u2 header_id = get_u2le(extra_p);
    const u2 data_size = get_u2le(extra_p);
    const u1 *extra = extra_p;
    extra_p += data_size;
    if (header_id == ZIP64_EXTRA_FIELD_TAG) {
      if (*uncompressed_size == U4_MAX) {
        *uncompressed_size = get_u8le(extra);
      }
      if (*compressed_size == U4_MAX) {
        *compressed_size = get_u8le(extra);
      }
      if (*offset == U4_MAX) {
        *offset = get_u8le(extra);
      }
    }
  }
  p += file_comment_length;
```

**File:** third_party/ijar/zip.cc (L766-769)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```

**File:** src/tools/singlejar/output_jar_simple_test.cc (L1179-1236)
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
