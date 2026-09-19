### Title
Unbounded heap over-read while parsing untrusted ZIP/JAR central directory entries in ijar - (File: third_party/ijar/zip.cc)

### Summary
`InputZipFile::ProcessCentralDirEntry` in `third_party/ijar/zip.cc` parses each Central Directory Header (CDH) of a ZIP/JAR file without ever verifying that the declared `file_name_length`, `extra_field_length`, or `file_comment_length` fields (and the fixed 46-byte CDH prefix itself) stay within the bounds of the mmap'd input file, unlike the sibling local-file-header parser `ProcessLocalFileEntry`, which explicitly bounds-checks every read via `EnsureRemaining()`.

### Finding Description
`ProcessLocalFileEntry` (third_party/ijar/zip.cc:332) calls `EnsureRemaining()` [1](#0-0)  before every variable-length read (`file_name`, `extra_field`, `file_data`), guaranteeing the cursor `p` never advances past `zipdata_in_ + input_file_->Length()`.

`ProcessCentralDirEntry`, however, reads the 46-byte fixed CDH fields, then `file_name_length`, `extra_field_length`, `file_comment_length` bytes, and additionally walks a loop over extra-field tag/length pairs (`header_id`, `data_size`) purely based on attacker-controlled length fields, with **no equivalent bounds check at all**: [2](#0-1) 

The only sanity check that exists (`FindZipCentralDirectory`) validates that the trailing EOCD comment reaches exactly to the end of file, and separately trusts `cd.central_dir_size`/`cd.central_dir_offset` from the (attacker-controlled) EOCD/EOCD64 record without cross-checking that the sum of per-entry `file_name_length + extra_field_length + file_comment_length + 46` for every entry actually stays inside `[central_dir_, bytes + in_length)`. `CalculateOutputLength()` and `ProcessNext()` both loop calling `ProcessCentralDirEntry` purely driven by the (unchecked) 4-byte signature match, so a crafted entry with an oversized `extra_field_length`/`file_comment_length` (or a CDH placed near the very end of the mapped region) drives `p`/`extra_p` past the end of the mmap, producing an out-of-bounds heap read analogous to the Wireshark SRVLOC dissector's missing bounds check that CVE-2019-10899 patched.

### Impact Explanation
`ijar` is Bazel's interface-jar generator, invoked on `.jar` inputs during Java compilation (e.g. `java_import`, transitively processing jars fetched via `http_jar`/`http_file`/Maven-style external repositories). Because the file being parsed is a byte-for-byte artifact an attacker fully controls (a hostile origin server or malicious dependency release), and because a declared `sha256`/`integrity` on the download only pins that exact byte stream (it does nothing to validate the internal ZIP structure), an attacker who publishes a matching-hash-but-malformed `.jar` can trigger this out-of-bounds read, crashing the Bazel build worker (denial of service) or, depending on heap layout, leaking adjacent heap bytes into JVM class-file processing metadata.

### Likelihood Explanation
Moderate. It requires the attacker-supplied artifact to be consumed via a code path that runs `ijar` (any `java_import`/JVM external-dependency setup does this by default), and requires crafting a ZIP with an out-of-range `extra_field_length`/`file_comment_length` or with truncated content near the CDH the parser deliberately trusts. No credential access, MITM, or root-repo Starlark trust is required — only the ability to host a `.jar` at a URL a build consumes.

### Recommendation
Add the same `EnsureRemaining()` bounds check used in `ProcessLocalFileEntry` to `ProcessCentralDirEntry` before every read of `file_name_length`, `extra_field_length`, `file_comment_length` bytes and before the internal extra-field walk (`header_id`/`data_size`), rejecting the file with an error instead of advancing `p`/`extra_p` past `zipdata_in_ + input_file_->Length()`.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` style repro: construct a minimal valid ZIP whose single CDH declares `extra_field_length = 0xFFFF` while only a handful of bytes actually follow before the mapped file ends (matching the technique already used in `CreateZipWithMalformedExtraField()` in `src/tools/singlejar/output_jar_simple_test.cc:1179-1225`, but targeting `third_party/ijar`'s `ProcessCentralDirEntry` instead of singlejar's `ExtraField::find`, which — unlike ijar — already bounds-checks each extra field against `end`). Feed this archive as a `java_import` jar (or run the `ijar` binary directly) and observe the out-of-bounds read/crash when `CalculateOutputLength()`/`ProcessNext()` iterate the malformed central directory entry.

### Citations

**File:** third_party/ijar/zip.cc (L158-170)
```text
  // Check that at least n bytes remain in the input file, otherwise
  // abort with an error message.  "state" is the name of the field
  // we're about to read, for diagnostics.
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

**File:** third_party/ijar/zip.cc (L493-545)
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
  p += file_comment_length;
  return true;
}
```
