### Title
Missing null-terminator on jar manifest buffer causes out-of-bounds heap read in `IsMultiRelease` - (File: `third_party/ijar/ijar.cc`)

### Summary
`ijar` (the interface-jar stripper Bazel runs on every `.jar` it processes, including third-party jars fetched via `http_jar`/`http_file`/Maven-style external repositories or produced by an untrusted CI branch's compile step) reads `META-INF/MANIFEST.MF` out of an attacker-controlled ZIP entry into a heap buffer that is allocated with *exactly* the entry's compressed/uncompressed size and is never null-terminated, then scans that buffer with `strchr`, a null-terminator-seeking libc function. This is structurally the same bug class as CVE-2025-11683 in YAML::Syck: a buffer copied from untrusted input is handed to a C string-scanning routine without guaranteeing termination, so the scan can run past the allocation into adjacent heap memory.

### Finding Description
`ManifestLocator::Process` copies the raw bytes of the `META-INF/MANIFEST.MF` ZIP entry into a heap buffer sized to the exact entry length, with no reserved terminator byte: [1](#0-0) 

That buffer and its exact size are later passed to `IsMultiRelease`, which walks lines using `strchr(line_start, '\n')`: [2](#0-1) 

`strchr` does not know about `manifest_size`/`data_end` — it scans byte-by-byte until it finds either `'\n'` or a `'\0'` terminator, wherever that first occurs in memory. If the attacker crafts a manifest entry whose last line has no trailing `'\n'` (an entirely legal, if malformed, `MANIFEST.MF`, e.g. the file simply ends after `Multi-Release: true` with no newline, or after any other final attribute line), `line_end` from `strchr` is computed from a call that has already scanned past the end of the `manifest_size`-byte allocation looking for the next `\n` or `\0` byte, reading adjacent heap memory. The subsequent `line_length = line_end - line_start` and `strncmp`/index comparisons (`line_start[enabled_length]`) can then read and compare bytes taken from beyond the buffer, and the resulting boolean (`multi_release`) — which controls whether a `Multi-Release: true` line is echoed into the *output* interface jar's manifest — leaks a bit of information derived from adjacent heap content into build outputs, and, depending on heap layout, can trigger a crash/OOB read fault. This is the direct analog of the disclosed CVE: "missing null-terminators … causes out-of-bounds read and potential information disclosure," reachable "with complex … files with a hash of all keys and empty values" — here, a manifest with a missing trailing newline.

This is reachable purely from **content an unprivileged attacker publishes**: any `.jar`/`.srcjar` fetched from an external URL, Maven artifact, or produced by compiling an untrusted branch is passed through `ijar` as part of normal Bazel Java/JVM rule processing (`java_import`, `java_library` interface-jar generation, etc.). No checksum verification is expected to catch this because the sha256 pin on the archive (if any) only verifies the *whole archive's* bytes match a known-good hash — it says nothing about the *content* of a manifest an attacker is free to craft arbitrarily while still matching that hash (i.e., the attacker controls the archive from the start, there's no tampering of a trusted archive involved).

### Impact Explanation
- Out-of-bounds heap read of adjacent allocator memory in a native tool (`ijar`) invoked during ordinary Bazel Java builds.
- The read outcome influences whether `Multi-Release: true` is written into the generated interface jar's manifest, which is an observable side channel — an attacker who can trigger repeated builds and observe the resulting `ijar` output (e.g., via CI artifacts) could infer information about adjacent heap bytes over repeated builds, though the leaked channel is only 1 bit of information (multi-release yes/no) per invocation, limiting practical exfiltration bandwidth.
- Worse case: a read that lands on an unmapped page ends the `ijar` process (denial of the current build unit), which the scan rules explicitly exclude as a standalone finding, so the primary in-scope impact here is the low-bandwidth information-disclosure side channel and the general memory-safety violation itself (heap OOB read), consistent with the CVE's own assessed severity (Medium, no confirmed access to memory outside the module's allocation).

### Likelihood Explanation
Any manifest file lacking a final trailing `\n` on its last attribute line reaches this code path unconditionally whenever `ijar` runs on a jar containing `META-INF/MANIFEST.MF` — this requires no special ZIP corruption, just an off-spec (but easily crafted, and not obviously invalid) manifest inside an attacker-supplied jar. There is no length/format validation of manifest content before `IsMultiRelease` runs, and no existing containment or checksum mechanism inspects the *semantic* correctness of manifest line termination.

### Recommendation
Ensure the manifest buffer is null-terminated (or at minimum, bound the scan strictly by `manifest_size`) before or during `IsMultiRelease`:
- Allocate `manifest_size + 1` bytes in `ManifestLocator::Process`, write a trailing `'\0'`, and store `manifest_size` as before, or
- Replace `strchr(line_start, '\n')` with a bounded scan such as `static_cast<const char*>(memchr(line_start, '\n', data_end - line_start))`, which is impossible to run past `data_end`.

### Proof of Concept
A JUnit/`ijar_test.sh`-style reproduction:
1. Build a jar (e.g., with `zip`/`jar` tool or `GenJarWithManifestSections.java`, which already exists in `third_party/ijar/test/`) whose `META-INF/MANIFEST.MF` entry's last byte is not `\n` (e.g., ends in `Multi-Release: true` with no trailing newline, and the ZIP entry's declared uncompressed size matches exactly, so no padding trails it).
2. Run `ijar` (as invoked from `third_party/ijar/test/ijar_test.sh`) on this jar under AddressSanitizer/Valgrind.
3. Observe a heap-buffer-overflow (read) report from `strchr` inside `IsMultiRelease`, confirming the OOB read past the `manifest_buf_` allocation from `ManifestLocator::Process`. [3](#0-2) [4](#0-3)

### Citations

**File:** third_party/ijar/ijar.cc (L73-91)
```text
class ManifestLocator : public devtools_ijar::ZipExtractorProcessor {
 public:
  ManifestLocator() : manifest_buf_(nullptr), manifest_size_(0) {}
  virtual ~ManifestLocator() { free(manifest_buf_); }

  u1* manifest_buf_;
  size_t manifest_size_;

  virtual bool Accept(const char* filename, const u4 /*attr*/) {
    return strcmp(filename, MANIFEST_PATH) == 0;
  }

  virtual void Process(const char* /*filename*/, const u4 /*attr*/,
                       const u1* data, const size_t size) {
    manifest_buf_ = (u1*)malloc(size);
    memmove(manifest_buf_, data, size);
    manifest_size_ = size;
  }
};
```

**File:** third_party/ijar/ijar.cc (L223-249)
```text
static bool IsMultiRelease(u1* manifest_data, size_t manifest_size) {
  static const char* enabled_line = "Multi-Release: true";
  static const size_t enabled_length = strlen(enabled_line);
  const char* line_start = (const char*)manifest_data;
  const char* data_end = (const char*)manifest_data + manifest_size;
  while (line_start < data_end) {
    const char* line_end = strchr(line_start, '\n');
    line_end = line_end != nullptr ? line_end + 1 : data_end;
    size_t line_length = line_end - line_start;

    if (line_length >= enabled_length &&
        strncmp(line_start, enabled_line, enabled_length) == 0 &&
        (line_length == enabled_length || line_start[enabled_length] == '\r' ||
         line_start[enabled_length] == '\n')) {
      return true;
    }
    line_start = line_end;
  }
  return false;
}

void JarStripperProcessor::WriteManifest(
    const ManifestLocator& manifest_locator, const char* target_label,
    const char* injecting_rule_kind) {
  bool multi_release = manifest_locator.manifest_buf_ != nullptr &&
                       IsMultiRelease(manifest_locator.manifest_buf_,
                                      manifest_locator.manifest_size_);
```
