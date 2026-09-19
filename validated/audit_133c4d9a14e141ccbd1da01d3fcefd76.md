### Title
Integer underflow in `InputZipFile::CalculateOutputLength` from attacker-controlled zip64 central-directory sizes leads to undersized output buffer for `ijar` - (File: `third_party/ijar/zip.cc`)

### Summary
`ijar` (the interface-jar generator used when Bazel processes any `.jar` input, including jars fetched via `http_jar`/`http_archive`/`http_file` from an attacker-controlled URL) estimates the size of its output buffer from unsigned 64-bit arithmetic on values taken directly from the attacker-supplied ZIP central directory, with no validation that `compressed_size <= uncompressed_size` or that computed subtractions cannot underflow, analogous to the unchecked `gasUsedDelta`/`baseFeeDelta` subtraction in the reported Solidity issue.

### Finding Description
`InputZipFile::ProcessCentralDirEntry` reads `compressed_size` / `uncompressed_size` (and their 64-bit zip64 overrides) verbatim from each central directory entry with no relationship check between the two values: [1](#0-0) 

`InputZipFile::CalculateOutputLength` then sums these attacker-controlled, unsigned (`u8`) values across all entries and computes:
```
return (u8) input_file_->Length() - skipped_compressed_size
    + (uncompressed_size - compressed_size);
``` [2](#0-1) 

Because `u8` is unsigned, if the attacker sets a "kept" entry's declared `compressed_size` larger than its declared `uncompressed_size` (trivial to do — these are just header fields, not validated against the actual number of decompressed bytes), `uncompressed_size - compressed_size` wraps around to a huge value near `UINT64_MAX`. Combined with the `- skipped_compressed_size` term, an attacker who controls all of these header fields can drive the final `output_length` to essentially any value they choose, including a small value that is far less than what will actually be written.

This value flows unchecked into the output file allocation: [3](#0-2) 

`ZipBuilder::Create(file_out, output_length)` uses `output_length` as `estimated_size` to `ftruncate()` and `mmap()` the output file (see `MappedOutputFile` constructor, which sizes the mapping directly from the caller-supplied estimate): [4](#0-3) 

The actual bytes later written into that mmap'd region during `ProcessAll()`/`JarStripperProcessor::Process`/`JarCopierProcessor::Process` are based on the real decompressed entry contents (via `memcpy`/`WriteStr`/`AppendTargetLabelToManifest`), not on the corrupted size estimate: [5](#0-4) [6](#0-5) 

If the underflowed estimate produces a mapping smaller than what is actually needed to hold the real (correctly-sized) entries, writes past the mmap'd region become out-of-bounds writes (heap/mmap buffer overflow), corrupting adjacent memory or crashing the process that builds the interface jar. This is a genuine "untrusted content stays data" invariant violation: a value from an attacker-served archive is used, unchecked, to size a memory region into which real (attacker- or build-derived) file content is subsequently written.

### Impact Explanation
This affects the `ijar` tool, invoked as part of ordinary Java build actions whenever a `.jar` is processed to strip it to an interface jar. Since `.jar` inputs commonly originate from `http_jar`/`http_archive` rules pointed at attacker-controlled or compromised mirror URLs (a hostile-origin-server scenario this scan explicitly allows when integrity is not correctly enforced for this internal computation), a maliciously crafted JAR whose central directory declares inconsistent `compressed_size`/`uncompressed_size` fields can cause an out-of-bounds write into process memory of the `ijar` binary during the build, rather than a mere `ijar` correctness bug. This is memory corruption in a build tool driven entirely by untrusted archive bytes, not merely a DoS.

### Likelihood Explanation
Likelihood is moderate-to-high for exposure: any `.jar` file consumed by a build (from a dependency fetched over the network, an untrusted branch, or a compromised mirror) reaches this code path automatically as part of the standard Java toolchain's use of `ijar`. Constructing a ZIP whose header fields declare `compressed_size > uncompressed_size` for an accepted (`.class`) entry requires no special exploitation skill — it is a matter of hand-crafting the local/central-directory header bytes, which is well within reach of an unprivileged attacker publishing a jar dependency.

### Recommendation
In `InputZipFile::CalculateOutputLength` (and in `ProcessCentralDirEntry`), validate that `compressed_size <= uncompressed_size` for every entry before accumulating them, and use saturating/checked arithmetic (e.g., reject or clamp on underflow) instead of raw unsigned subtraction. Additionally, consider having `ZipBuilder`/`MappedOutputFile` grow or re-validate the output mapping dynamically against the actual bytes written rather than trusting a single unchecked size estimate computed purely from attacker-supplied header fields.

### Proof of Concept
A reproducible proof would be a `BuildIntegrationTestCase`/`src/test/shell` test that:
1. Hand-crafts a `.jar`/`.zip` file with one accepted `.class` entry whose central directory declares `uncompressed_size = 10` and `compressed_size = 0xFFFFFFFF` (or a zip64 extra field with `compressed_size` deliberately larger than `uncompressed_size`), while the real (decompressed) `.class` payload written by `Process()` is large.
2. Runs `ijar` (or a Java rule that invokes it, e.g. `java_library` depending on such a crafted jar via `http_jar`) and observes a crash (SIGSEGV/heap corruption) or `ASAN`/`MSAN` report of an out-of-bounds write in `OutputZipFile`/`MappedOutputFile`, rather than a clean "corrupt zip" rejection.

I was not able to trace the exact bounds-checking (or lack thereof) inside `OutputZipFile::NewFile`/`FinishFile` write paths within the available index (the relevant `zip.cc` write-path code beyond line ~900 and the full `zip.h`/`MappedOutputFile` growth logic were not fully retrievable), so I cannot fully confirm whether any additional guard exists there that would stop the OOB write in practice. This should be verified directly in a full checkout before treating the impact as certain; I'd recommend starting a Devin session with full repository access to inspect `third_party/ijar/zip.cc` (lines ~900 onward) and `third_party/ijar/zip.h` to confirm the absence of bounds checks in `OutputZipFile::NewFile`/`FinishFile` and to build the PoC.

### Citations

**File:** third_party/ijar/zip.cc (L507-541)
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

**File:** third_party/ijar/ijar.cc (L179-203)
```text
void JarStripperProcessor::Process(const char *filename, const u4 /*attr*/,
                                   const u1 *data, const size_t size) {
  if (verbose) {
    fprintf(stderr, "INFO: StripClass: %s\n", filename);
  }
  if (IsModuleInfo(filename) || IsKotlinModule(filename, strlen(filename)) ||
      IsScalaTasty(filename, strlen(filename))) {
    u1 *q = builder_->NewFile(filename, 0);
    memcpy(q, data, size);
    builder_->FinishFile(size, /* compress: */ false, /* compute_crc: */ true);
  } else {
    u1 *buf = reinterpret_cast<u1 *>(malloc(size));
    u1 *classdata_out = buf;
    if (!StripClass(buf, data, size)) {
      free(classdata_out);
      return;
    }
    u1 *q = builder_->NewFile(filename, 0);
    size_t out_length = buf - classdata_out;
    memcpy(q, classdata_out, out_length);
    builder_->FinishFile(out_length, /* compress: */ false,
                         /* compute_crc: */ true);
    free(classdata_out);
  }
}
```

**File:** third_party/ijar/ijar.cc (L290-303)
```text
void JarCopierProcessor::Process(const char *filename, const u4 /*attr*/,
                                 const u1 *data, const size_t size) {
  if (verbose) {
    fprintf(stderr, "INFO: CopyFile: %s\n", filename);
  }
  // We already handled the manifest in WriteManifest
  if (strcmp(filename, MANIFEST_DIR_PATH) == 0 ||
      strcmp(filename, MANIFEST_PATH) == 0) {
    return;
  }
  u1 *q = builder_->NewFile(filename, 0);
  memcpy(q, data, size);
  builder_->FinishFile(size, /* compress: */ false, /* compute_crc: */ true);
}
```

**File:** third_party/ijar/ijar.cc (L430-437)
```text
  u8 output_length = in->CalculateOutputLength();
  if (output_length < JAR_WITH_DUMMY_FILE_SIZE) {
    output_length = JAR_WITH_DUMMY_FILE_SIZE;
  }
  output_length +=
      EstimateManifestOutputSize(target_label, injecting_rule_kind);

  std::unique_ptr<ZipBuilder> out(ZipBuilder::Create(file_out, output_length));
```

**File:** third_party/ijar/mapped_file_unix.cc (L88-122)
```text
MappedOutputFile::MappedOutputFile(const char* name, size_t estimated_size)
    : estimated_size_(estimated_size) {
  impl_ = NULL;
  opened_ = false;
  int fd = open(name, O_CREAT|O_RDWR|O_TRUNC, 0644);
  if (fd < 0) {
    snprintf(errmsg, MAX_ERROR, "open(): %s", strerror(errno));
    errmsg_ = errmsg;
    return;
  }

  // Create mmap-able sparse file
  if (ftruncate(fd, estimated_size) < 0) {
    snprintf(errmsg, MAX_ERROR, "ftruncate(): %s", strerror(errno));
    errmsg_ = errmsg;
    return;
  }

  // Ensure that any buffer overflow in JarStripper will result in
  // SIGSEGV or SIGBUS by over-allocating beyond the end of the file.
  size_t mmap_length =
      std::min(static_cast<size_t>(estimated_size + sysconf(_SC_PAGESIZE)),
               std::numeric_limits<size_t>::max());
  void* mapped =
      mmap(NULL, mmap_length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  if (mapped == MAP_FAILED) {
    snprintf(errmsg, MAX_ERROR, "mmap(): %s", strerror(errno));
    errmsg_ = errmsg;
    return;
  }

  impl_ = new MappedOutputFileImpl();
  impl_->fd_ = fd;
  impl_->mmap_length_ = mmap_length;
  buffer_ = reinterpret_cast<u1*>(mapped);
```
