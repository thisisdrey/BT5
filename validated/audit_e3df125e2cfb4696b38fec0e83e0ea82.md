### Title
Out-of-bounds read when parsing crafted Zip64 "extra field" data in `InputZipFile::ProcessCentralDirEntry` - (File: `third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in the `ijar` tool (also used transitively via the shared zip-header format consumed by `singlejar`) parses the "extra fields" region of a ZIP central directory entry with no bounds checking against the declared extra-field length, and reads a fixed 8-byte `Zip64ExtraField` payload without validating that the field actually contains 8 bytes. This mirrors the htslib CRAM rANS codec bug class: a length-prefixed sub-record inside an attacker-controlled binary container is trusted to determine how many bytes to consume, with no check that consumption stays inside the buffer that was validated to hold the record.

### Finding Description
`ProcessCentralDirEntry` reads the central directory header, then walks the "extra fields" blob using a completely unguarded loop: [1](#0-0) 

Two problems compound here:
1. `p += extra_field_length` is trusted, and `EnsureRemaining` is never called for `extra_field_length` in this function (unlike `ProcessLocalFileEntry`, which does call `EnsureRemaining` before advancing `p` for `file_name_length_`/`extra_field_length_` — see [2](#0-1) ). `ProcessCentralDirEntry` has no equivalent check, so `extra_field_length` can push `p` past the mapped file.
2. The inner `while (extra_p != p)` loop reads a 2-byte `header_id` and a 2-byte `data_size` from attacker-controlled bytes and does `extra_p += data_size`. If `data_size` does not evenly divide the declared `extra_field_length`, `extra_p` can jump past `p` instead of landing exactly on it, and the loop condition `extra_p != p` never becomes false — the loop continues consuming and interpreting arbitrary heap/mmap memory as further "extra field" headers, well beyond the entry's declared boundary.
3. Even when `header_id == ZIP64_EXTRA_FIELD_TAG` is hit legitimately, `get_u8le(extra)` unconditionally reads 8 bytes from `extra`, without checking that `data_size` for that specific sub-field is actually ≥ 8. A crafted `data_size` of 0 or 1 still causes an 8-byte read past the intended sub-field.

`get_u4le`/`get_u2le`/`get_u8le` (defined in `third_party/ijar/common.h`) perform no bounds checks themselves — they simply dereference and advance the pointer: [3](#0-2) 

Because the central directory (and thus this function) is invoked once per entry from `InputZipFile::ProcessNext` and `CalculateOutputLength`, a single crafted entry is enough to trigger the unchecked read on every zip file this code processes: [4](#0-3)  and [5](#0-4) 

### Impact Explanation
`ijar` (interface-jar stripping) and the zip/JAR-header format it defines are exercised on JAR files that are build inputs (e.g., pre-built/third-party `.jar` files brought in via `java_import`, `http_jar`, or similar external dependencies). While the overall archive bytes are typically pinned by a `sha256`/`integrity` value at fetch time, that hash only certifies the byte stream as a whole — it says nothing about the internal ZIP central-directory/extra-field structure being well-formed. An attacker who controls the *original* content published at a URL (and thus also controls/publishes the corresponding sha256 that a victim's `MODULE.bazel`/`WORKSPACE` would pin) can embed a Zip64 extra field with an inconsistent `data_size` to make `ProcessCentralDirEntry` walk off the end of the extra-fields blob (and, transitively, potentially past the end of the mmap'd input file), corrupting `InputZipFile`'s parsed state (sizes/offsets) with attacker-influenced out-of-bounds bytes. This is a memory-safety violation in a native (C++) build tool that a victim's `bazel build` invokes over untrusted archive content — the type of bug class the analog report highlights (untrusted length fields driving buffer traversal in a native decoder), even though this repo does not contain the htslib code itself.

### Likelihood Explanation
Reaching this code only requires supplying a `.jar`/`.zip` file with a malformed extra field to any Bazel build step that runs `ijar` or reads the shared zip-header definitions (e.g. via `java_import`/interface-jar generation, or singlejar deploy-jar assembly) — both are default, unconditional parts of the Java toolchain, not opt-in flags. No credentials, privileged access, or race conditions are needed; only control over the bytes of a JAR dependency is required, which is exactly the "unprivileged content publisher" attacker model described.

### Recommendation
Add explicit bounds checks in `InputZipFile::ProcessCentralDirEntry`:
- Call `EnsureRemaining(extra_field_length, "extra_field")` (mirroring `ProcessLocalFileEntry`) before advancing `p` past the extra-fields region.
- In the extra-field walking loop, verify `extra_p + 4 <= p` before reading `header_id`/`data_size`, and verify `extra_p + data_size <= p` before advancing, aborting/erroring instead of looping past `p` on mismatch.
- Before calling `get_u8le(extra)` for the Zip64 tag, verify `data_size >= 8` (and ideally that the specific attribute offset requested is within `data_size`).

### Proof of Concept
Construct a ZIP central directory entry (as in the existing `CreateZipWithMalformedExtraField` test helper pattern) where the extra-fields blob has `extra_field_length` bytes advertised, but the first extra-field record's `payload_size`/`data_size` does not sum to `extra_field_length` (e.g., declares a `data_size` larger than the remaining bytes, or a Zip64 tag with `payload_size < 16`). Feed this archive to `ijar` (or any `singlejar`/`OutputJar` code path that parses `CDH`/extra fields) and observe out-of-bounds reads/ASan violations when `extra_p` overshoots `p` in the `while (extra_p != p)` loop or when `get_u8le` reads past a truncated Zip64 field, analogous to the existing `MalformedExtraField` regression test that exercises a similar oversized-`payload_size` scenario for local-header extra fields: [6](#0-5)

### Citations

**File:** third_party/ijar/zip.cc (L302-330)
```text
bool InputZipFile::ProcessNext() {
  // Process the next entry in the central directory. Also make sure that the
  // content pointer is in sync.
  u8 compressed, uncompressed;
  u8 offset;
  if (!ProcessCentralDirEntry(central_dir_current_, &compressed, &uncompressed,
                              filename, PATH_MAX, &attr, &offset)) {
    return false;
  }

  // There might be an offset specified in the central directory that does
  // not match the file offset, so always update our pointer.
  p = zipdata_in_ + in_offset_ + offset;

  if (EnsureRemaining(4, "signature") < 0) {
    return false;
  }
  u4 signature = get_u4le(p);
  if (signature == LOCAL_FILE_HEADER_SIGNATURE) {
    if (ProcessLocalFileEntry(compressed, uncompressed) < 0) {
      return false;
    }
  } else {
    error("local file header signature for file %s not found\n", filename);
    return false;
  }

  return true;
}
```

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

**File:** third_party/ijar/zip.cc (L550-581)
```text
u8 InputZipFile::CalculateOutputLength() {
  const u1* current = central_dir_;

  u8 compressed_size = 0;
  u8 uncompressed_size = 0;
  u8 skipped_compressed_size = 0;
  u4 attr;
  u8 offset;
  char filename[PATH_MAX];

  while (true) {
    u8 file_compressed, file_uncompressed;
    if (!ProcessCentralDirEntry(current,
                                &file_compressed, &file_uncompressed,
                                filename, PATH_MAX, &attr, &offset)) {
      break;
    }

    if (processor->Accept(filename, attr)) {
      compressed_size += (u8) file_compressed;
      uncompressed_size += (u8) file_uncompressed;
    } else {
      skipped_compressed_size += file_compressed;
    }
  }

  // The worst case is when the output is simply the input uncompressed. The
  // metadata in the zip file will stay the same, so the file will grow by the
  // difference between the compressed and uncompressed sizes.
  return (u8) input_file_->Length() - skipped_compressed_size
      + (uncompressed_size - compressed_size);
}
```

**File:** third_party/ijar/common.h (L49-72)
```text
inline u2 get_u2le(const u1 *&p) {
    u4 x = (p[1] << 8) | p[0];
    p += 2;
    return x;
}

inline u4 get_u4be(const u1 *&p) {
    u4 x = (p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3];
    p += 4;
    return x;
}

inline u4 get_u4le(const u1 *&p) {
    u4 x = (p[3] << 24) | (p[2] << 16) | (p[1] << 8) | p[0];
    p += 4;
    return x;
}

inline u8 get_u8le(const u1 *&p) {
  u4 lo = get_u4le(p);
  u4 hi = get_u4le(p);
  u8 x = ((u8)hi << 32) | lo;
  return x;
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
