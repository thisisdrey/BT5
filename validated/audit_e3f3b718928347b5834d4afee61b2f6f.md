Based on my analysis, I found a strong analog to CVE-2020-11088 in Bazel's `ijar` tool, which parses attacker-controlled ZIP central-directory entries with fixed-offset, length-prefixed fields much like FreeRDP's `ntlm_read_NegotiateMessage`.

### Title
Out-of-bound read parsing ZIP central directory entries in `InputZipFile::ProcessCentralDirEntry` - (File: `third_party/ijar/zip.cc`)

### Summary
`ijar` is Bazel's C++ tool used to strip a `.jar` down to an interface jar (invoked for every Java dependency compile-jar, via `java_common.run_ijar` / the Java rules) [1](#0-0) . It reads jar bytes via `InputZipFile`, mmap'ing the whole file and walking the ZIP central directory with `ProcessCentralDirEntry` [2](#0-1) .

### Finding Description
`ProcessCentralDirEntry` reads `file_name_length`, `extra_field_length`, and `file_comment_length` directly out of the (attacker-supplied) central directory header bytes and then advances the cursor `p` by these attacker-controlled sizes without any bounds check against the actual mapped file size: [3](#0-2) 
It copies `file_name_length` bytes from `p` into a fixed-size buffer and walks `extra_p`/`data_size` pairs entirely unchecked: [4](#0-3) 
Contrast this with `ProcessLocalFileEntry`, which does call `EnsureRemaining()` before trusting `file_name_length_`/`extra_field_length_` [5](#0-4) . `EnsureRemaining` is the bounds-check primitive available in this class [6](#0-5) , but it is never called inside `ProcessCentralDirEntry`. The comment on the function claims "the central directory is always followed by another data structure that has a signature, so parsing it this way is safe" [7](#0-6) , but that assumption does not hold if `file_name_length`, `extra_field_length`, or `file_comment_length` are crafted to point past the end of the mmap'ed region — the code will read (and `memcpy`) from unmapped/out-of-file memory.

The test file confirms researchers are already aware "malformed extra field" inputs are a concern for the sibling `singlejar` ZIP parser (`zip_headers.h`, which does perform explicit `ExtraField::find` bounds checks) [8](#0-7)  — but `ijar`'s `zip.cc` `ProcessCentralDirEntry` extra-field loop has no equivalent check (`extra_p != p` loop just trusts `data_size`) [9](#0-8) .

### Impact Explanation
An attacker who controls the content of a `.jar` file that ends up as a Bazel dependency (e.g. served from a compromised/malicious Maven repository, `http_jar`/`http_archive` origin, or an untrusted branch whose CI runs Bazel) can craft a ZIP central directory with oversized `file_name_length`/`extra_field_length`/`file_comment_length` fields. Since `ijar` runs natively in-process against mmap'ed memory with no bounds validation in this path, this can cause an out-of-bounds read (crash/DoS via SIGSEGV, or potential information disclosure if the OOB bytes are echoed into the output jar's filename/central-directory copy) during a normal build. Because integrity/checksum verification for `http_archive`/`http_jar` only validates the archive as a whole (the byte-for-byte content, not its internal structure), a hostile origin can supply a byte sequence that legitimately matches a declared `sha256`/integrity value while still being a malformed ZIP that triggers this OOB read in `ijar`.

### Likelihood Explanation
Requires only that a Java dependency's jar (attacker-published) be processed by the built-in `ijar`/`run_ijar` action, which happens automatically for any prebuilt/imported jar used as a compile dependency — no special build flags are needed. The attack surface is broad (any registry/mirror serving Java jars used by `java_import`/`java_library` deps).

### Recommendation
Add explicit bounds checks (`EnsureRemaining`) in `InputZipFile::ProcessCentralDirEntry` before trusting `file_name_length`, `extra_field_length`, and `file_comment_length`, mirroring what `ProcessLocalFileEntry` already does and what `zip_headers.h`'s `ExtraField::find` does for `singlejar`. The extra-field walking loop should validate that each `header_id`/`data_size` pair stays within the declared `extra_field_length` and within the mapped file bounds before dereferencing.

### Proof of Concept
A JUnit/shell reproduction would construct a minimal ZIP with one central-directory entry whose `file_name_length` (or `extra_field_length`) is set larger than the remaining bytes in the mmap'ed file (analogous to the `CreateZipWithMalformedExtraField()` helper already present in `src/tools/singlejar/output_jar_simple_test.cc` [8](#0-7) , but targeting `third_party/ijar/zip.cc`'s `InputZipFile`/`ijar` binary instead of singlejar), then invoke the `ijar` binary on it and observe a crash (ASAN heap-buffer-overflow) rather than a clean parse error.

Note: I was not able to directly view `InputZipFile::Open()` / EOCD parsing (to confirm there is no earlier bound placed on `central_dir_current_`), since that portion of `zip.cc` was not returned by search; this should be verified in a full read of the file before finalizing severity.

### Citations

**File:** third_party/ijar/README.txt (L37-41)
```text
Details:

  ijar is a tool that reads a .jar file and emits a .jar file
  containing only the parts that are relevant to Java compilation.
  For example, it throws away:
```

**File:** third_party/ijar/zip.cc (L161-170)
```text
  int EnsureRemaining(size_t n, const char *state) {
    size_t in_offset = p - zipdata_in_;
    size_t remaining = input_file_->Length() - in_offset;
    if (n > remaining) {
      return error("Premature end of file (at offset %zd, state=%s); "
                   "expected %zd more bytes but found %zd.\n",
                   in_offset, state, n, remaining);
    }
    return 0;
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

**File:** third_party/ijar/zip.cc (L486-492)
```text
// - whether the entry is a class file (to be included in the output).
// Precondition: p points to the beginning of an entry in the central dir
// Postcondition: p points to the beginning of the next entry in the central dir
// Returns true if the central directory contains another file and false if not.
// Of course, in the latter case, the size output variables are not changed.
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
```

**File:** third_party/ijar/zip.cc (L493-542)
```text
bool InputZipFile::ProcessCentralDirEntry(const u1 *&p, u8 *compressed_size,
                                          u8 *uncompressed_size, char *filename,
                                          size_t filename_size, u4 *attr,
                                          u8 *offset) {
  u4 signature = get_u4le(p);

  if (signature != CENTRAL_FILE_HEADER_SIGNATURE) {
    if (signature != DIGITAL_SIGNATURE && signature != EOCD_SIGNATURE &&
        signature != ZIP64_EOCD_SIGNATURE) {
      error("invalid central file header signature: 0x%x\n", signature);
    }
    return false;
  }

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
```

**File:** src/tools/singlejar/output_jar_simple_test.cc (L1179-1224)
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
```
