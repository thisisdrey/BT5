## Title
Out-of-bounds read in ijar's ZIP central directory extra-field parser via crafted `data_size` — (File: `third_party/ijar/zip.cc`)

## Summary
`InputZipFile::ProcessCentralDirEntry()` in ijar (the tool used to build stripped interface JARs for Java compilation) walks a ZIP central-directory entry's "extra field" block using a length (`extra_field_length`) taken directly from the archive, but iterates the sub-fields inside that block with **no bounds checking on the individual `data_size` values** it reads, exactly analogous to the GStreamer `vprp` bug where a per-record size is trusted without validating it against the remaining buffer.

## Finding Description
The vulnerable loop: [1](#0-0) 

```
p += file_name_length;
const u1 *extra_p = p;
p += extra_field_length;
while (extra_p != p) {
  const u2 header_id = get_u2le(extra_p);
  const u2 data_size = get_u2le(extra_p);
  const u1 *extra = extra_p;
  extra_p += data_size;
  if (header_id == ZIP64_EXTRA_FIELD_TAG) {
    if (*uncompressed_size == U4_MAX) *uncompressed_size = get_u8le(extra);
    if (*compressed_size == U4_MAX) *compressed_size = get_u8le(extra);
    if (*offset == U4_MAX) *offset = get_u8le(extra);
  }
}
```

`extra_field_length` is a 2-byte value taken verbatim from the central directory header at [2](#0-1) , with no check that it (or the file bounds it implies) actually fits in the mapped input. Inside the loop, `data_size` is likewise attacker-controlled and is never validated against `extra_p`'s remaining distance to `p` (the extra-field-block boundary) before `get_u8le(extra)` reads 8 bytes from `extra`. If `data_size` is crafted such that `extra_p + data_size` overshoots `p`, the loop condition `extra_p != p` can skip past the intended terminator (looping until it happens to equal a later address, or reading far past the mapped file), and `get_u8le(extra)` can read 8 bytes when fewer than 8 (or 0) bytes of legitimate payload remain — an out-of-bounds read structurally identical to the GStreamer `vprp` flaw, where a controlled field is used to size the walk instead of validating each individual field/record against the buffer end.

Notably, ijar's sibling implementation in `src/tools/singlejar/zip_headers.h` (`ExtraField::find`) explicitly guards against this exact class of bug: [3](#0-2) 

checking `byte_ptr(start) + sizeof(ExtraField) > end` and `byte_ptr(start) + extra_field->size() > end` before dereferencing — the equivalent checks are absent in `third_party/ijar/zip.cc`'s central-directory extra-field walk.

The comment at line 491-492 asserts this parsing is "safe" because the central directory is always followed by another signature-bearing structure, but that assumption does not bound the inner per-field `data_size` walk, nor does it prevent `p`/`extra_p` from being advanced past the actual mapped file region if `extra_field_length` or `data_size` exceed the true remaining bytes.

## Impact Explanation
`ijar` is the Bazel-bundled tool that strips class-file bodies out of JARs to produce interface JARs used for Java compilation actions; it is invoked on JAR files supplied by the build, including precompiled/third-party JARs pulled in via `java_import`-style rules or downloaded artifacts. A JAR served by a malicious dependency URL, package registry mirror, or an untrusted branch that a victim's build fetches and feeds into `ijar` can trigger an out-of-bounds read, crashing the Bazel build worker (denial of service) and potentially leaking adjacent heap memory content into observable state such as generated interface JAR bytes, decompression error paths, or crash artifacts.

## Likelihood Explanation
No checksum, size, or containment check exists between the field length taken from the archive and the memory-mapped bounds of the input file for this specific extra-field walk, so any attacker who can get a crafted JAR into the build (a downloaded dependency, cached artifact, or file on an untrusted CI branch) can trigger the read deterministically with a single crafted central-directory entry.

## Recommendation
Bound every extra-field sub-record read to the enclosing extra-field block and to the actual end of the mapped input file before advancing `extra_p` or dereferencing `extra`: verify `extra_p + 4 <= p` before reading `header_id`/`data_size`, and verify `extra_p + data_size <= p` (and within the mapped buffer) before calling `get_u8le(extra)`, matching the bounds-checking pattern already implemented in `ExtraField::find` in `src/tools/singlejar/zip_headers.h`.

## Proof of Concept
Construct a ZIP whose single central directory entry declares `extra_field_length = 4` but with an extra field payload beginning with `header_id = ZIP64_EXTRA_FIELD_TAG (0x0001)`, `data_size = 0xFFFF` (or any value larger than the 4 remaining declared bytes), and `compressed_size`/`uncompressed_size`/`offset` fields set to `0xFFFFFFFF` (`U4_MAX`) to force the ZIP64 branch to execute `get_u8le(extra)`. Feed this archive to the `ijar` binary (`third_party/ijar/ijar.cc` entry point invoking `ZipExtractor`) as a `BuildIntegrationTestCase`/`src/test/shell/bazel` style test that runs `ijar <crafted.jar> <out.jar>`; under ASan the process should report a heap-buffer-overflow read in `InputZipFile::ProcessCentralDirEntry` at the `get_u8le(extra)` call.

### Citations

**File:** third_party/ijar/zip.cc (L510-511)
```text
  u2 file_name_length = get_u2le(p);
  u2 extra_field_length = get_u2le(p);
```

**File:** third_party/ijar/zip.cc (L523-542)
```text
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

**File:** src/tools/singlejar/zip_headers.h (L99-115)
```text
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
