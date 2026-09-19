### Title
Heap buffer over-read / overflow when ijar merges an attacker-controlled JAR's `MANIFEST.MF` due to non-NUL-terminated buffer treated as a C string - (File: `third_party/ijar/ijar.cc`)

### Summary
`ijar`, Bazel's interface-jar generator, extracts a dependency JAR's `META-INF/MANIFEST.MF` into a heap buffer that is **not NUL-terminated**, then processes that buffer with NUL-terminated-string functions (`strchr`, `strncmp`) while merging in `Target-Label`/`Injecting-Rule-Kind` attributes. If the untrusted manifest's final line lacks a trailing `\n`, the scan runs past the allocation, and the resulting out-of-bounds "line length" is then `memcpy`'d into a fixed-size output buffer whose capacity was estimated from the *original* manifest size — producing both an out-of-bounds heap read and a write overflow into the output ZIP buffer.

### Finding Description
`ManifestLocator::Process` copies the manifest entry's bytes verbatim, with no terminator: [1](#0-0) 

`IsMultiRelease` and `JarCopierProcessor::AppendTargetLabelToManifest` then walk this buffer using `strchr(line_start, '\n')`, which has no notion of the buffer's true end and will read past it looking for a `'\n'` byte (or crash on an unmapped page): [2](#0-1) [3](#0-2) 

If `strchr` happens to find a `'\n'` byte in adjacent heap memory beyond `manifest_data + size`, `line_end - line_start` yields a length larger than what remains of the real manifest, and that over-long region is `memcpy`'d into `buf` (line 364) — both reading heap memory that does not belong to the manifest and writing more bytes into the output buffer than were ever accounted for.

The output buffer's capacity comes from `EstimateManifestOutputSize`, which only budgets for the declared target-label/rule-kind strings and small fixed constants — **not** for any possible over-read length: [4](#0-3) 

For the "merge into existing manifest" branch used by `JarCopierProcessor::WriteManifest`, the size actually reserved for manifest content assumes the copy is bounded by the *original* entry size (as counted by `ZipExtractor::CalculateOutputLength`); the OOB length computed via `strchr` is not bounded by that assumption at all, so the `memcpy` in `AppendTargetLabelToManifest` can write past the reserved region.

The one place ijar's authors acknowledge overflow risk is on the *output* mmap, where a single guard page is reserved specifically to turn overflows into a crash rather than corruption: [5](#0-4) 
This guard page does not protect the separate heap allocation `manifest_buf_` (a plain `malloc`), so the read side of the bug is unmitigated, and depending on how far past the true manifest end the stray `'\n'` is found, the write side can still exceed the one-page slack.

### Impact Explanation
An unprivileged attacker who publishes/serves a JAR that a victim's build consumes (e.g. as an `http_jar`, remote/Maven dependency, or any pinned artifact whose *sha256/integrity is honored but whose content is otherwise attacker-authored*) can craft a `MANIFEST.MF` whose last "attribute" line has no trailing newline. Because the checksum only verifies that the bytes match what was published — not that the content is memory-safe to parse — the pinned/verified artifact itself is the attack vector. When `ijar` (invoked by Bazel to build the corresponding interface jar for `java_import`/toolchain processing) processes this manifest:
- it reads out of bounds on the heap (potential crash / information disclosure of adjacent heap bytes into the produced interface JAR's manifest, since those bytes get `memcpy`'d into the output), and
- it can write past the intended bounds of the output ZIP buffer, corrupting adjacent output data structures.

This is a genuine memory-safety violation triggered purely by content bytes inside a dependency that passed its integrity check, matching the CVE's bug class (unsafe string handling of untrusted, non-guaranteed-terminated input causing heap corruption before any further validation).

### Likelihood Explanation
Reaching this code only requires Bazel to build an interface jar from a JAR with `--target_label`/merge processing enabled (the default `ijar` invocation path used for every non-stripped/copied jar with manifest merging, `JarCopierProcessor`), and for the attacker to control the manifest's raw bytes. Manifest files are attacker-fully-controlled once they control the archive; no cooperation from Bazel internals or the network layer is required, and checksums do not inspect manifest structure at all.

### Recommendation
- NUL-terminate (or otherwise bound every scan by an explicit end pointer, never relying on `strchr`/`strncmp`/`strcmp` semantics) the `manifest_buf_` buffer in `ManifestLocator::Process`.
- Replace `strchr(line_start, '\n')` in `IsMultiRelease` and `AppendTargetLabelToManifest` with a bounded search (e.g. `memchr(line_start, '\n', data_end - line_start)`), and clamp all derived lengths to `data_end - line_start` before any `memcpy`.
- Recompute `EstimateManifestOutputSize`/output buffer sizing to be provably an upper bound of every code path that can write into the manifest output buffer, rather than assuming the merge case never exceeds the original entry size.

### Proof of Concept
A `BuildIntegrationTestCase`/shell reproduction:
1. Build a malicious `dep.jar` containing `META-INF/MANIFEST.MF` whose content is exactly `size` bytes with **no trailing `\n`** and no `\r\n` line terminator on the final "attribute" (e.g. ends with `Foo: bar` with no newline, sized so the allocation is immediately followed by attacker/heap-controlled bytes without an early `'\n'`).
2. Reference `dep.jar` from a Bazel `java_import`/`java_library` target (or run `ijar --target_label //x:x dep.jar out-interface.jar` directly, matching `OpenFilesAndProcessJar`'s `JarCopierProcessor` path).
3. Run under ASan/heap-checker (`src/tools/singlejar`-style test harness or a small C++ unit test that calls `ManifestLocator`+`JarCopierProcessor::AppendTargetLabelToManifest` directly with a manually `malloc`'d, non-terminated buffer) and observe a heap-buffer-overflow read reported by ASan when `strchr`/`strncmp` walk past `manifest_size_`, and, depending on heap layout, a corresponding out-of-bounds write into the `ZipBuilder` output buffer.

### Citations

**File:** third_party/ijar/ijar.cc (L85-90)
```text
  virtual void Process(const char* /*filename*/, const u4 /*attr*/,
                       const u1* data, const size_t size) {
    manifest_buf_ = (u1*)malloc(size);
    memmove(manifest_buf_, data, size);
    manifest_size_ = size;
  }
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

**File:** third_party/ijar/ijar.cc (L345-368)
```text
u1 *JarCopierProcessor::AppendTargetLabelToManifest(
    u1 *buf, const u1 *manifest_data, const size_t size,
    const char *target_label, const char *injecting_rule_kind) {
  const char *line_start = (const char *)manifest_data;
  const char *data_end = (const char *)manifest_data + size;

  // Write main attributes part
  while (line_start < data_end && line_start[0] != '\r' &&
         line_start[0] != '\n') {
    const char *line_end = strchr(line_start, '\n');
    // Go past return char to point to next line, or to end of data buffer
    line_end = line_end != nullptr ? line_end + 1 : data_end;

    // Copy line unless it's Target-Label/Injecting-Rule-Kind and we're writing
    // that ourselves
    if (strncmp(line_start, TARGET_LABEL_KEY, TARGET_LABEL_KEY_LENGTH) != 0 &&
        strncmp(line_start, INJECTING_RULE_KIND_KEY,
                INJECTING_RULE_KIND_KEY_LENGTH) != 0) {
      size_t len = line_end - line_start;
      memcpy(buf, line_start, len);
      buf += len;
    }
    line_start = line_end;
  }
```

**File:** third_party/ijar/ijar.cc (L386-408)
```text
static size_t EstimateManifestOutputSize(const char *target_label,
                                         const char *injecting_rule_kind) {
  if (target_label == nullptr) {
    return 0;
  }
  // local headers
  size_t length = 30 * 2 + MANIFEST_DIR_PATH_LENGTH + MANIFEST_PATH_LENGTH;
  // central directory
  length += 46 * 2 + MANIFEST_DIR_PATH_LENGTH + MANIFEST_PATH_LENGTH;
  // zip64 EOCD entries
  length += 56 * 2;

  // manifest content
  length += MANIFEST_HEADER_LENGTH;
  length += MULTI_RELEASE_TRUE_LINE_LENGTH;
  // target label manifest entry, including newline
  length += TARGET_LABEL_KEY_LENGTH + strlen(target_label) + 2;
  if (injecting_rule_kind) {
    // injecting rule kind manifest entry, including newline
    length += INJECTING_RULE_KIND_KEY_LENGTH + strlen(injecting_rule_kind) + 2;
  }
  return length;
}
```

**File:** third_party/ijar/mapped_file_unix.cc (L106-112)
```text
  // Ensure that any buffer overflow in JarStripper will result in
  // SIGSEGV or SIGBUS by over-allocating beyond the end of the file.
  size_t mmap_length =
      std::min(static_cast<size_t>(estimated_size + sysconf(_SC_PAGESIZE)),
               std::numeric_limits<size_t>::max());
  void* mapped =
      mmap(NULL, mmap_length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
```
