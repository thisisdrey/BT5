Confirmed: `get_u2le`/`get_u4le`/`get_u8le` in `third_party/ijar/common.h:39-72` perform raw pointer dereferences with zero bounds checking — all safety must come from the caller.

### Title
Heap buffer over-read in `InputZipFile::ProcessCentralDirEntry` when parsing malicious ZIP/JAR central directory records - (File: `third_party/ijar/zip.cc`)

### Summary
`ijar`, invoked via `java_common.run_ijar`/`stamp_jar` on arbitrary (including prebuilt/externally-fetched) `.jar` files, parses the ZIP central directory without validating attacker-controlled length fields against the mmap'd input buffer bounds, unlike its sibling function for local file headers.

### Finding Description
`InputZipFile::ProcessCentralDirEntry` [1](#0-0)  reads `compressed_size`, `uncompressed_size`, `file_name_length`, `extra_field_length`, `file_comment_length`, `attr`, and `offset` directly from the mmap'd input buffer via `get_u4le`/`get_u2le`, then advances the cursor `p` by `file_name_length`, `extra_field_length`, and `file_comment_length` and memcpy's `file_name_length` bytes into a fixed `filename[PATH_MAX]` buffer — all without any call to `EnsureRemaining`, the bounds-check helper that the sibling function `ProcessLocalFileEntry` correctly calls before every variable-length read [2](#0-1) .

The extra-field loop is especially exposed: [3](#0-2) 
`data_size` (attacker-controlled, from `get_u2le(extra_p)`) is used to advance `extra_p` and, for the ZIP64 tag, `get_u8le(extra)` reads 8 bytes from `extra` with no check that `extra + 8` stays within `extra_p`'s claimed field, the overall `extra_field_length`, or the mapped file end. Since `get_u2le`/`get_u4le`/`get_u8le` (`third_party/ijar/common.h:39-72`) are raw, unchecked pointer reads, a crafted central directory entry with an oversized `file_name_length`, `extra_field_length`, `file_comment_length`, or a ZIP64 extra field claiming a length that runs past the actual mmap'd file causes a heap-based (mmap-based) buffer over-read — the same bug class as CVE-2018-13870 (unchecked attacker-supplied length field driving structured-record parsing past the buffer boundary).

`ijar` is reachable on content that is not necessarily produced by `javac`: `java_common.run_ijar`/`stamp_jar` operate on any `File` passed to them [4](#0-3) , including prebuilt jars from `java_import`/`http_jar`/Maven-resolved artifacts. `http_jar`/`http_file`/`maven_install`-style rules do not mandate `sha256`/`integrity` pinning; when omitted (a common, default-flag-compatible configuration), the fetched jar bytes are entirely attacker-controlled by whoever serves the URL/registry entry, and there is no digest to "fail" — verification simply never runs.

### Impact Explanation
An out-of-bounds heap/mmap read in a build-time helper process (`ijar`) can crash the build (denial of build), and in the worst case leak adjacent process memory content into computed values (e.g., the ZIP64-derived `compressed_size`/`uncompressed_size`/`offset`) that influence subsequent output-jar length calculations (`CalculateOutputLength`, `third_party/ijar/zip.cc:550-581`) or crafted central-directory bytes emitted into the interface jar, constituting an information-disclosure-adjacent memory-safety bug reachable purely from untrusted archive content.

### Likelihood Explanation
Moderate-to-high: any Bazel Java build that consumes a prebuilt `.jar` from an external, attacker-influenceable source (unpinned `http_jar`, compromised/malicious registry mirror, or a build over an untrusted branch/PR that adds a `java_import` pointing at attacker-supplied jar bytes) and then runs `ijar`/`stamp_jar` on it (default for `java_library`/`java_import` dependency-jar generation) will invoke this unguarded parser on fully attacker-controlled bytes.

### Recommendation
Add `EnsureRemaining` (or equivalent length-vs-remaining-bytes) checks in `ProcessCentralDirEntry` before consuming `file_name_length`, `extra_field_length`, `file_comment_length`, and before/while iterating the extra-field loop (validating `data_size` against the remaining extra-field bytes and the overall mapped buffer end) before any `get_u2le`/`get_u4le`/`get_u8le` call, mirroring the checks already present in `ProcessLocalFileEntry`.

### Proof of Concept
A `BuildIntegrationTestCase`/`src/test/shell/bazel` style test analogous to the existing singlejar regression test `CreateZipWithMalformedExtraField` (`src/tools/singlejar/output_jar_simple_test.cc:1179-1225`, which already guards against exactly this bug class in singlejar) but targeting `ijar`: construct a minimal ZIP central directory entry whose `extra_field_length`/ZIP64 extra field `payload_size` exceeds the actual buffer, feed it to `ijar` (e.g. via a `java_import` pointing at the crafted jar and building with `--nowarn` under ASan), and confirm ASan reports a heap-buffer-overflow (read) inside `InputZipFile::ProcessCentralDirEntry`/`get_u8le`, whereas a corresponding local-file-header fuzz input is correctly rejected via `EnsureRemaining`.

### Citations

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

**File:** docs/versions/8.7.0/rules/lib/toplevel/java_common.mdx (L120-136)
```text
## run_ijar

```
File java_common.run_ijar(actions, *, jar, target_label=None, java_toolchain)
```

Runs ijar on a jar, stripping it of its method bodies. This helps reduce rebuilding of dependent jars during any recompiles consisting only of simple changes to method implementations. The return value is typically passed to `JavaInfo#compile_jar`.

### Parameters

| Parameter | Description |
| --- | --- |
| `actions` | [actions](../builtins/actions); required |
| `jar` | [File](../builtins/File); required  The jar to run ijar on. |
| `target_label` | [Label](../builtins/Label); or `None`; default is `None`  A target label to stamp the jar with. Used for `add_dep` support. Typically, you would pass `ctx.label` to stamp the jar with the current rule's label. |
| `java_toolchain` | Info; required  A JavaToolchainInfo to used to find the ijar tool. |

```
