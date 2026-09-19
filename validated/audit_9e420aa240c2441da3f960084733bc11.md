### Title
Out-of-bounds read when parsing a malicious ZIP central directory entry in `InputZipFile::ProcessCentralDirEntry` - (File: `third_party/ijar/zip.cc`)

### Summary
`third_party/ijar/zip.cc` implements a minimal ZIP reader used by ijar/singlejar-adjacent tooling to read entries from attacker-supplied ZIP/JAR archives. Central directory entry parsing trusts length fields taken directly from attacker-controlled bytes without validating them against the mapped file bounds, unlike the local file header parser in the same file, which explicitly checks remaining bytes before advancing.

### Finding Description
`InputZipFile::ProcessCentralDirEntry` reads a central directory header's `file_name_length`, `extra_field_length`, and `file_comment_length` fields directly from the mmap'd input and then advances the cursor `p` by those attacker-controlled 16-bit values with no bounds check against the actual mapped extent of the file: [1](#0-0) 

This contrasts with `InputZipFile::ProcessLocalFileEntry`, which calls `EnsureRemaining()` before reading variable-length `file_name` and `extra_field` regions: [2](#0-1) 

The only global bound enforced is that the *aggregate* central directory (`cd.central_dir_offset + cd.central_dir_size`) fits inside the file, computed once in `FindZipCentralDirectory`: [3](#0-2) 

That check validates the *total* declared size of the central directory against the file length, but it does not validate that each individual entry's `file_name_length`/`extra_field_length`/`file_comment_length` fields are internally consistent with the space actually available. An attacker who crafts a ZIP/JAR with a central directory whose *declared* `central_dir_size` matches the file's real remaining bytes, but whose *individual entries* declare inflated length fields (e.g. a large `file_name_length` on the last entry), causes `p` in `ProcessCentralDirEntry`/`CalculateOutputLength`/`ProcessNext` to be advanced past the actual end of the mapped input buffer. Because `memcpy(filename, p, len)` at line 520 and the subsequent `get_u4le`/`get_u2le` reads in the next iteration's `ProcessNext`/`ProcessCentralDirEntry` dereference `p` without any additional remaining-length check, this results in an out-of-bounds read of the mmap'd region (potential crash or leak of adjacent memory content, mirroring the OOB-read struct-conversion bug class of CVE-2017-17507, where a crafted structural length field in the input format is trusted without validation).

The comment at line 491-492 asserts "the central directory is always followed by another data structure that has a signature, so parsing it this way is safe" — this assumption only holds for a well-formed archive; a maliciously crafted one can violate it by manipulating per-entry length fields while keeping the aggregate declared size self-consistent.

### Impact Explanation
This affects Bazel's own archive/JAR processing tooling (`third_party/ijar`), which is invoked when Bazel reads JARs (e.g., ijar/singlejar processing of dependency jars or output jars) — content that can originate from an external, untrusted dependency (an artifact fetched from a repository/mirror). An out-of-bounds read can crash the Bazel-invoked tool (denial of service for that build step) or, depending on adjacent memory layout, leak bytes from adjacent process memory into subsequent processing/output. It does not grant arbitrary write or code execution capability by itself, and it stays within a single process's memory space (no cross-build cache poisoning or credential exfiltration is demonstrated here).

### Likelihood Explanation
Reaching this requires an attacker who can supply a crafted ZIP/JAR file that Bazel's tooling processes directly (matching the "unprivileged content producer" profile — e.g., a malicious `.jar` published at a dependency URL). It requires precise crafting of the ZIP central directory but no privileged access. Existing checksum/integrity mechanisms (e.g. `sha256`/`integrity` on `http_archive`) protect the *whole archive byte stream* used for extraction via the tar/zip decompressors used in Starlark repository rules, but `third_party/ijar/zip.cc` is a separate, lower-level reader used for JAR introspection (ijar) that is not gated by the same checksum validation path and processes the archive's internal structure directly.

### Recommendation
Add explicit bounds checking in `ProcessCentralDirEntry` (analogous to `EnsureRemaining` in `ProcessLocalFileEntry`) before reading and advancing past `file_name_length`, `extra_field_length`, and `file_comment_length`, validating that `p + these lengths` does not exceed the mapped input buffer's end (`zipdata_in_ + input_file_->Length()`), and returning an error rather than continuing to parse if violated.

### Proof of Concept
A concrete PoC would require constructing a ZIP file where:
1. The EOCD's `central_dir_size`/`central_dir_offset` are self-consistent with the file length (passing `FindZipCentralDirectory`'s check).
2. The last central directory entry declares a `file_name_length` (or `extra_field_length`/`file_comment_length`) larger than the actual bytes remaining before the EOCD/file end.

This would need to be exercised through a `BuildIntegrationTestCase`/`src/test/shell/bazel` test invoking ijar or singlejar against such a crafted jar and observing a crash/ASAN heap-buffer-overflow report from `ProcessCentralDirEntry`. I was not able to build and run such a PoC in this read-only investigation — this should be verified by a background agent with test-execution access (e.g., building `third_party/ijar` with ASAN and feeding it a crafted ZIP as described).

**Note on investigation limits:** I could not fully trace how `zip.cc`'s `InputZipFile` is invoked from ijar's/singlejar's higher-level entry points (e.g., whether any wrapping code enforces additional bounds before calling into this reader), since those call sites were not returned by search. A background Devin session with full repository and build access would be needed to confirm the exact reachable entry point (ijar CLI vs. singlejar) and produce a runnable ASAN-based reproduction.

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

**File:** third_party/ijar/zip.cc (L507-525)
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
```

**File:** third_party/ijar/zip.cc (L766-777)
```text
  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }

  // Do not change output values before determining that they are OK.
  *offset = cd.central_dir_offset;
  // Central directory start can then be used to determine the actual
  // starts of the zip file (which can be different in case of a non-zip
  // header like for auto-extractable binaries).
  *central_dir = end_of_central_dir - cd.central_dir_size;
  return true;
```
