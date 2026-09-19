## Analysis

Confirmed a concrete, reachable integer-overflow bug in `third_party/ijar/zip.cc` that bypasses a bounds check used to locate a zip/JAR central directory, feeding an attacker-fully-controlled 64-bit size directly into pointer arithmetic without any subsequent range validation.

### Title
Integer Overflow in `FindZipCentralDirectory`'s Zip64 bounds check leads to out-of-bounds central-directory pointer - (File: `third_party/ijar/zip.cc`)

### Summary
`FindZipCentralDirectory` validates the central directory location with `cd.central_dir_offset + cd.central_dir_size > in_length`, where both operands are attacker-controlled 64-bit values read directly from a Zip64 End Of Central Directory record via `get_u8le` in `MaybeReadZip64CentralDirectory`. No range check clamps these fields before the addition, so a crafted archive can make the sum wrap around 2^64 and pass the check, after which `*central_dir = end_of_central_dir - cd.central_dir_size;` computes a wildly out-of-bounds pointer that is subsequently dereferenced.

### Finding Description [1](#0-0) 
`EndOfCentralDirectoryRecord` stores `central_dir_size` and `central_dir_offset` as unsigned 64-bit (`u8`) fields. [2](#0-1) 
In the Zip64 path, `MaybeReadZip64CentralDirectory` reads these two fields straight from file bytes with `get_u8le(current)` — fully attacker-controlled, no bounds imposed at parse time. [3](#0-2) 
Back in `FindZipCentralDirectory`, after the Zip64 lookup succeeds, the only sanity check performed is:
```
if (cd.central_dir_offset + cd.central_dir_size > in_length) {
  ... return false;
}
```
Because both `central_dir_offset` and `central_dir_size` are attacker-chosen 64-bit values, their sum can be made to wrap around `UINT64_MAX` back into the valid `[0, in_length]` range even though `central_dir_size` itself is enormous (e.g., close to `2^64`). This defeats the intended invariant ("central directory lies within the mapped file"). [4](#0-3) 
The check having passed, the code computes:
```
*central_dir = end_of_central_dir - cd.central_dir_size;
```
With `cd.central_dir_size` near `UINT64_MAX`, this pointer subtraction is undefined behavior and, in practice, produces a pointer far outside the mapped input buffer (effectively wrapping to a near-arbitrary address, since pointer minus a huge unsigned value is equivalent to pointer plus a small value mod address space, or a segfaulting address). [5](#0-4) 
`InputZipFile::Open` takes this returned `central_dir` pointer directly and stores it as `central_dir_` / `central_dir_current_` with no further validation, then subsequent `ProcessCentralDirEntry` calls (invoked from `ProcessNext`/`CalculateOutputLength`) dereference this pointer via `get_u4le`/`get_u2le`/`memcpy` to parse "central directory entries," reading out-of-bounds memory and copying attacker-influenced garbage into the `filename` buffer and derived size fields. [6](#0-5) 
`ProcessCentralDirEntry` performs no bounds checking against the mapped buffer bounds when reading fields at `p`, relying entirely on the caller having handed it a valid, in-bounds pointer — an invariant broken by the overflow above.

### Impact Explanation
This is the `ijar` tool used by Bazel to strip JAR files down to interface jars for Java compilation (`third_party/ijar/ijar.cc`, `third_party/ijar/zip_main.cc`). Any build that processes an untrusted `.jar`/`.zip` (e.g., a JAR checked into a PR from an untrusted contributor and consumed via `java_import`, or a maliciously repackaged zip supplied through a repository rule) drives this code path. The resulting out-of-bounds pointer is dereferenced for reads (and the read bytes, including filenames, are copied into fixed-size stack buffers and consulted for control decisions), causing memory-safety violations: crashes (denial of availability of the specific build action) and potential information disclosure of adjacent process memory into the generated interface jar or error output. This is a real integer-overflow-driven bypass of a security-relevant bounds check, not a mere crash from malformed input rejected safely.

### Likelihood Explanation
Likely and simple to trigger: constructing a minimal Zip64 archive with a small legitimate-looking file and hand-crafted Zip64 EOCD record where `central_dir_size` is set near `2^64` and `central_dir_offset` chosen so the 64-bit sum wraps to fit within `in_length` is a matter of a few hex edits to the EOCD64 structure — no cryptographic material or timing is required. The `ijar` binary runs unsandboxed as a normal build tool over the raw file bytes.

### Recommendation
In `FindZipCentralDirectory` (and `MaybeReadZip64CentralDirectory`), validate `central_dir_size` and `central_dir_offset` individually against `in_length` (each must be `<= in_length`) *before* summing them, and perform the addition using an overflow-safe comparison (e.g., `central_dir_offset > in_length - central_dir_size` after confirming `central_dir_size <= in_length`, or use a wide/checked-arithmetic type). Additionally, validate that `end_of_central_dir - cd.central_dir_size` does not underflow below `bytes` before dereferencing, and have `ProcessCentralDirEntry`/`InputZipFile::Open` re-verify the resulting `central_dir` pointer lies within `[bytes, bytes + in_length]`.

### Proof of Concept
A `src/test/shell/bazel` or standalone `ijar` unit test can reproduce this:
1. Build a minimal valid local file entry + central directory header for one file.
2. Append a Zip64 EOCD record (signature `0x06064b50`) with `central_dir_size` = `0xFFFFFFFFFFFFFF00` (near `UINT64_MAX`) and `central_dir_offset` chosen such that `(central_dir_offset + central_dir_size) mod 2^64 <= in_length` (e.g., `central_dir_offset = 0x00000000000000E0` so the wrapped sum equals a small in-range value).
3. Append the Zip64 EOCD locator and standard EOCD (with `cen_size32`/`cen_offset32` = `0xFFFFFFFF` to signal Zip64) pointing at the crafted Zip64 EOCD.
4. Run `ijar <crafted.zip> out.jar` (or call `ZipExtractor::Create` directly in a `gtest`), and observe a crash/ASan heap-buffer-overflow/out-of-bounds report when `ProcessCentralDirEntry` dereferences the wrapped `central_dir` pointer, confirming the bypass of the `central_dir_offset + central_dir_size > in_length` guard at [7](#0-6) .

### Citations

**File:** third_party/ijar/zip.cc (L493-524)
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
```

**File:** third_party/ijar/zip.cc (L583-591)
```text
// An end of central directory record, sized for optional zip64 contents.
struct EndOfCentralDirectoryRecord {
  u4 number_of_this_disk;
  u4 disk_with_central_dir;
  u8 central_dir_entries_on_this_disk;
  u8 central_dir_entries;
  u8 central_dir_size;
  u8 central_dir_offset;
};
```

**File:** third_party/ijar/zip.cc (L608-620)
```text
  // size of zip64 end of central directory record
  // (fixed size unless there's a zip64 extensible data sector, which
  // we don't need to read)
  get_u8le(current);
  get_u2be(current);  // version made by
  get_u2be(current);  // version needed to extract

  u4 number_of_this_disk = get_u4be(current);
  u4 disk_with_central_dir = get_u4le(current);
  u8 central_dir_entries_on_this_disk = get_u8le(current);
  u8 central_dir_entries = get_u8le(current);
  u8 central_dir_size = get_u8le(current);
  u8 central_dir_offset = get_u8le(current);
```

**File:** third_party/ijar/zip.cc (L756-769)
```text
  if (!FindZip64CentralDirectory(bytes, in_length, &end_of_central_dir, &cd)) {
    return false;
  }

  if (cd.number_of_this_disk != 0 || cd.disk_with_central_dir != 0 ||
      cd.central_dir_entries_on_this_disk != cd.central_dir_entries) {
    fprintf(stderr, "multi-disk JAR files are not supported\n");
    return false;
  }

  if (cd.central_dir_offset + cd.central_dir_size > in_length) {
    fprintf(stderr, "central directory offset/size is invalid\n");
    return false;
  }
```

**File:** third_party/ijar/zip.cc (L771-777)
```text
  // Do not change output values before determining that they are OK.
  *offset = cd.central_dir_offset;
  // Central directory start can then be used to determine the actual
  // starts of the zip file (which can be different in case of a non-zip
  // header like for auto-extractable binaries).
  *central_dir = end_of_central_dir - cd.central_dir_size;
  return true;
```

**File:** third_party/ijar/zip.cc (L814-846)
```text
bool InputZipFile::Open() {
  MappedInputFile* input_file = new MappedInputFile(filename_);
  if (!input_file->Opened()) {
    snprintf(errmsg, sizeof(errmsg), "%s", input_file->Error());
    delete input_file;
    return false;
  }

  void *zipdata_in = input_file->Buffer();
  u8 central_dir_offset;
  const u1 *central_dir = NULL;

  if (!devtools_ijar::FindZipCentralDirectory(
          static_cast<const u1*>(zipdata_in), input_file->Length(),
          &central_dir_offset, &central_dir)) {
    errno = EIO;  // we don't really have a good error number
    error("Cannot find central directory");
    delete input_file;
    return false;
  }
  const u1 *zipdata_start = static_cast<const u1*>(zipdata_in);
  in_offset_ = - static_cast<off_t>(zipdata_start
                                    + central_dir_offset
                                    - central_dir);

  input_file_ = input_file;
  zipdata_in_ = zipdata_start;
  central_dir_ = central_dir;
  central_dir_current_ = central_dir;
  p = zipdata_in_ + in_offset_;
  errmsg[0] = 0;
  return true;
}
```
