## Finding: Heap Buffer Over-Read in `ijar`'s Manifest Parsing (`IsMultiRelease`)

### Title
Heap-based buffer over-read via unterminated `strchr()` scan in ijar manifest parsing - (File: `third_party/ijar/ijar.cc`)

### Summary
`ijar`, Bazel's interface-jar generator, extracts `META-INF/MANIFEST.MF` from any JAR it processes (including third-party JARs fetched via `http_jar`, `http_archive`/`maven_install`, or any `java_import`/`java_library` dependency) and scans it byte-by-byte using `strchr()`, a C-string function that assumes NUL termination. The extracted manifest buffer is *not* NUL-terminated, so a manifest crafted without a trailing newline on its last line causes `strchr()` to walk past the end of the heap allocation — the same bug class as CVE-2019-20005 in ezXML's `ezxml_decode`, where `strchr()` was run past the point where a string's logical content ended.

### Finding Description
`ManifestLocator::Process` extracts the raw manifest entry bytes from the JAR's central directory into a heap buffer sized to exactly the entry length, with no allocation for a terminating NUL: [1](#0-0) 

`IsMultiRelease` then treats that raw, non-NUL-terminated buffer as a C string and calls `strchr(line_start, '\n')` to find line boundaries: [2](#0-1) 

`strchr` has no length bound — it keeps reading byte-by-byte until it finds `'\n'` or a `'\0'`. Because `manifest_buf_` is exactly `manifest_size` bytes with no trailing NUL guaranteed, if the JAR's `MANIFEST.MF` entry ends without a final `\n` (a completely legal, if slightly malformed, manifest — many ZIP tools happily store arbitrary bytes as entry content), `strchr` reads past the end of the `malloc`'d buffer into adjacent heap memory until it happens to find a `\n` byte or crashes on an unmapped page. This mirrors CVE-2019-20005 exactly: a parser assumes a byte string is properly terminated where it isn't, and a libc string-scanning primitive over-reads the heap allocation as a result.

This is reached unconditionally whenever ijar builds an interface jar and a `META-INF/MANIFEST.MF` entry is present — `WriteManifest` calls `IsMultiRelease` for every jar processed: [3](#0-2) 

### Impact Explanation
A heap-based out-of-bounds read can leak adjacent heap memory content into ijar's control flow (deciding whether `Multi-Release: true` is (falsely or arbitrarily) written into the generated interface jar) or crash the ijar process (denial of the build for that target). While the class typically maps to memory disclosure/DoS rather than RCE, it is a genuine memory-safety violation triggered purely by content of a JAR entry that an unprivileged party controls (e.g., a JAR served from a dependency's download URL or artifact registry). The archive's overall checksum (if pinned) only verifies the byte-for-byte content of the JAR was fetched correctly — it does not, and cannot, prevent this bug, since the malicious manifest bytes are exactly what was requested and verified; the vulnerability is in how Bazel's native tool parses those verified bytes.

### Likelihood Explanation
Any build that consumes an externally-sourced JAR (via `http_jar`, `http_archive` unpacking a JAR, or a Maven/Bazel-central dependency) and compiles against it with `java_import`/`java_library` will invoke ijar on that JAR to produce the interface jar used for compilation. Producing a JAR whose `MANIFEST.MF` entry does not end in `\n` is trivial for any party who can publish or serve a JAR (a hostile origin server, compromised mirror, or malicious dependency version) — no special access to the victim's machine or credentials is required.

### Recommendation
Do not rely on the manifest buffer being NUL-terminated. Either:
- Allocate `manifest_size + 1` bytes and explicitly NUL-terminate the buffer in `ManifestLocator::Process`, or
- Rewrite `IsMultiRelease` to use a length-bounded scan (e.g., `memchr(line_start, '\n', data_end - line_start)`), never calling `strchr` on the raw buffer.

### Proof of Concept
Construct a JAR whose `META-INF/MANIFEST.MF` entry content is exactly `"Manifest-Version: 1.0"` with **no trailing newline** (and size it so it lands at the end of a heap page/arena for reliable reproduction under ASan). Feed this JAR to the `ijar` binary (or trigger via `java_import`/`http_jar` in a Bazel build). Under AddressSanitizer, `IsMultiRelease`'s `strchr(line_start, '\n')` call reads past the `malloc(manifest_size)` allocation boundary, which ASan reports as a heap-buffer-overflow (read). A JUnit/shell reproduction can be built as a `src/test/shell/bazel` integration test that packages such a crafted manifest into a jar consumed by `java_import`, then runs the ijar action under an ASan-instrumented `ijar` binary and asserts no heap-overflow is reported. [4](#0-3)

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

**File:** third_party/ijar/ijar.cc (L223-242)
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
```

**File:** third_party/ijar/ijar.cc (L244-249)
```text
void JarStripperProcessor::WriteManifest(
    const ManifestLocator& manifest_locator, const char* target_label,
    const char* injecting_rule_kind) {
  bool multi_release = manifest_locator.manifest_buf_ != nullptr &&
                       IsMultiRelease(manifest_locator.manifest_buf_,
                                      manifest_locator.manifest_size_);
```
