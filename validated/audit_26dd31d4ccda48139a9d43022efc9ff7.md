Confirmed: `ProcessCentralDirEntry` in `third_party/ijar/zip.cc` performs zero bounds validation against the mmap'd file's length while `ProcessLocalFileEntry` and `SkipFile`/`ProcessFile` explicitly call `EnsureRemaining()` before every read. This asymmetry is the direct analog to CVE-2017-7960's `cr_input_new_from_uri` heap over-read: a length-prefixed field (`file_name_length`, `extra_field_length`, `file_comment_length`) taken from attacker-controlled data is used to advance a cursor and `memcpy` from it without checking it stays inside the mapped buffer.

### Title
Heap buffer over-read in ijar's ZIP central directory parser via unchecked length fields - (`third_party/ijar/zip.cc`)

### Summary
`InputZipFile::ProcessCentralDirEntry` in ijar's ZIP reader trusts the 16-bit `file_name_length`/`extra_field_length`/`file_comment_length` fields taken directly from a downloaded, attacker-authored JAR's central directory and advances/dereferences the cursor `p` by those amounts with no bounds check against the mmap'd file length, unlike the sibling local-file-header parsing path which calls `EnsureRemaining()` before every read.

### Finding Description
`http_jar`/`jvm_import_external` download a JAR from an attacker-influenced URL and expose it as a `java_import` target [1](#0-0) . When such a JAR is later processed for interface-jar generation (`java_common.run_ijar`, backed by `third_party/ijar/ijar.cc` and `third_party/ijar/zip.cc`), the file is opened via `mmap` and its central directory is walked entry by entry with `InputZipFile::ProcessCentralDirEntry` [2](#0-1) .

That function reads the fixed 46-byte header fields and the variable-length `file_name_length`, `extra_field_length`, `file_comment_length` fields directly off the cursor `p`, then does:
```
memcpy(reinterpret_cast<void*>(filename), p, len);
...
p += file_name_length;
...
p += extra_field_length;
...
p += file_comment_length;
```
with **no call to `EnsureRemaining()`** and no check that `p` (or `p + len`) stays inside `[zipdata_in_, zipdata_in_ + input_file_->Length())`. This is in stark contrast to `InputZipFile::ProcessLocalFileEntry` and `InputZipFile::SkipFile`, which explicitly bound-check every read via `EnsureRemaining()` [3](#0-2) [4](#0-3) . The comment on line 491-492 asserting "the central directory is always followed by another data structure that has a signature, so parsing it this way is safe" is not actually enforced by any length or bounds check — it only holds for a well-formed archive, and a crafted archive with an oversized `file_name_length`/`extra_field_length`/`file_comment_length` near the end of the mmap'd region will walk `p` (and the `memcpy` source) past the end of the mapped file.

This mirrors CVE-2017-7960 in libcroco: a length taken from untrusted input is used to advance/read a cursor without validating it against the actual buffer bounds, producing a heap-based buffer over-read.

### Impact Explanation
An attacker who controls the bytes served at a JAR's declared download URL (a compromised origin/mirror, or any registry/URL a victim's `MODULE.bazel`/`WORKSPACE` points to) can craft a malformed but hash-matching JAR (the attacker chooses the sha256 to pin since they control the artifact) whose central directory entries declare oversized `file_name_length`/`extra_field_length`/`file_comment_length` values near the end of the file. When Bazel runs `ijar`/`run_ijar` (or any consumer of `InputZipFile::ProcessNext`/`CalculateOutputLength`) against this artifact, `ProcessCentralDirEntry` reads and `memcpy`s bytes past the end of the mmap'd input, causing a heap/mapping over-read. Depending on platform mmap behavior this can crash the build tool (denial of service) or, more importantly, leak adjacent heap memory bytes into the `filename` buffer, which is subsequently used and potentially embedded (via `Accept`/output naming) into the produced interface jar — an information disclosure of unrelated process memory into build output.

### Likelihood Explanation
Likely reachable whenever a repository rule downloads a JAR from a URL under attacker influence and that jar is subsequently used as a `java_import`/dependency subject to `ijar` processing (a very common path for Java toolchains, header/interface jar generation). No special privileges, MITM, or local access are required — only the ability to publish/serve the JAR content at the declared URL.

### Recommendation
Add the same `EnsureRemaining()`-style bounds validation used in `ProcessLocalFileEntry`/`SkipFile` to `ProcessCentralDirEntry`, checking that `p` plus each variable-length field (name, extra field, comment) does not exceed `zipdata_in_ + input_file_->Length()` before reading/memcpy'ing, and failing gracefully (returning an error) instead of silently reading out of bounds.

### Proof of Concept
A reproducible JUnit/`BuildIntegrationTestCase` or `src/test/shell/bazel` test would:
1. Build a minimal ZIP/JAR by hand where the last central directory entry declares `file_name_length` (or `extra_field_length`/`file_comment_length`) larger than the number of bytes actually remaining in the file after that field.
2. Feed this file to `ijar` (e.g., via `run_ijar` on a `java_import` whose jar is this crafted file, matching this file's own sha256 so it is a "download" scenario) and observe (under ASan/valgrind) a heap-buffer-overflow read report from `InputZipFile::ProcessCentralDirEntry`'s `memcpy`/`get_u4le`/`get_u2le` calls, or a crash/incorrect output when compared against the same test run without the oversized length fields. [5](#0-4) [6](#0-5)

### Citations

**File:** tools/build_defs/repo/http.bzl (L340-358)
```text
def _http_jar_impl(ctx):
    """Implementation of the http_jar rule."""
    source_urls = _get_source_urls(ctx)
    downloaded_file_name = ctx.attr.downloaded_file_name
    download_info = ctx.download(
        source_urls,
        "jar/" + downloaded_file_name,
        ctx.attr.sha256,
        canonical_id = ctx.attr.canonical_id or get_default_canonical_id(ctx, source_urls),
        auth = get_auth(ctx, source_urls),
        integrity = ctx.attr.integrity,
    )
    ctx.file("WORKSPACE", "workspace(name = \"{name}\")".format(name = ctx.name))
    ctx.file("jar/BUILD", _HTTP_JAR_BUILD.format(
        java_import_bzl = str(Label("@rules_java//java:java_import.bzl")),
        file_name = downloaded_file_name,
    ))

    return _update_integrity_attr(ctx, _http_jar_attrs, download_info)
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

**File:** third_party/ijar/zip.cc (L332-370)
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

**File:** third_party/ijar/zip.cc (L420-434)
```text
int InputZipFile::SkipFile(const bool compressed) {
  if (!compressed) {
    // In this case, compressed_size_ == uncompressed_size_ (since the file is
    // uncompressed), so we can use either.
    if (compressed_size_ != uncompressed_size_) {
      return error("compressed size != uncompressed size, although the file "
                   "is uncompressed.\n");
    }
  }

  if (EnsureRemaining(compressed_size_, "file_data") < 0) {
    return -1;
  }
  p += compressed_size_;
  return 0;
```

**File:** third_party/ijar/zip.cc (L491-545)
```text
// Note that the central directory is always followed by another data structure
// that has a signature, so parsing it this way is safe.
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
