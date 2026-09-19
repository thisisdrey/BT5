## Title
Integer-overflow bypass of the central-directory bounds check lets a malicious ZIP/JAR make ijar read outside the validated central-directory region — (File: third_party/ijar/zip.cc)

### Summary
`ijar` (Bazel's bundled zip/jar stripping tool, used e.g. by `java_import` to build interface jars from prebuilt/fetched `.jar` files) locates a zip's central directory in `FindZipCentralDirectory` and validates its extent with a single bounds check before treating that memory range as the trusted "central directory" region. That check is done with 64-bit unsigned addition that can wrap around, which is the same class of bug as the reported Solana issue: a bounds check that is supposed to guarantee an entire access stays inside one validated region, but which can be defeated by attacker-chosen values so the "region" pointer/extent ends up pointing partly or wholly outside what was actually validated.

### Finding Description
`FindZipCentralDirectory` reads (attacker-controlled, from the zip's End-Of-Central-Directory / Zip64 EOCD records) `cd.central_dir_offset` and `cd.central_dir_size`, both 64-bit (`u8`), and validates them with: [1](#0-0) 

```
if (cd.central_dir_offset + cd.central_dir_size > in_length) {
  fprintf(stderr, "central directory offset/size is invalid\n");
  return false;
}
...
*offset = cd.central_dir_offset;
*central_dir = end_of_central_dir - cd.central_dir_size;
```

`central_dir_offset` and `central_dir_size` are populated from the (Zip64-extended) EOCD fields via `MaybeReadZip64CentralDirectory`, which parses fully attacker-controlled 64-bit little-endian integers out of the archive: [2](#0-1) 

Because the check adds two independently attacker-controlled `uint64_t` values, an attacker can choose `central_dir_offset` near `UINT64_MAX` and a small `central_dir_size` (or vice versa) so that `central_dir_offset + central_dir_size` wraps modulo 2^64 to a small value, passing the `> in_length` check even though `central_dir_offset` itself is nowhere near a legitimate offset inside the file. This is structurally identical to the reported bug class: a check meant to guarantee "the whole access stays inside one validated region" is bypassed because the arithmetic used to validate the range boundary can overflow, and downstream code (`central_dir_`, `ProcessCentralDirEntry`) then treats the resulting out-of-bounds pointer/offset as if it were within the validated, contained region.

`central_dir_` is subsequently walked entry-by-entry by `ProcessCentralDirEntry`, which itself performs no additional bounds validation against the mapped file extent — it blindly dereferences `p` to read the signature, sizes, filename length, and extra-field data, and `memcpy`s attacker-controlled-length filename/extra-field bytes: [3](#0-2) 

Unlike `InputZipFile::ProcessLocalFileEntry`, which explicitly calls `EnsureRemaining()` before every read of local-file-header fields, `ProcessCentralDirEntry` has no equivalent guard — it relies entirely on `FindZipCentralDirectory`'s single addition check to have already fenced the central directory inside the mapped file. Once that fence is bypassed by the overflow, iteration over `central_dir_` can walk memory outside the actual mmap'd central-directory bytes.

### Impact Explanation
`ijar` runs over prebuilt jars supplied to `java_import` (and similar rules), which are commonly obtained from externally hosted artifacts (e.g. via `http_archive`/`http_file`). An attacker who controls the content of such an artifact (a hostile package/mirror publishing a jar whose bytes match its own advertised `sha256`/`integrity` value — checksum pinning does not stop a malicious author from shipping a corrupt ZIP structure of their own construction) can craft a ZIP whose EOCD/Zip64-EOCD records cause the offset+size overflow described above. This leads to `ProcessCentralDirEntry` reading and `memcpy`-ing data from outside the intended central-directory bounds, i.e., an out-of-bounds read relative to the mmap'd input file, with lengths and offsets fully attacker-influenced (`file_name_length`, `extra_field_length`, `data_size`, `get_u8le` values from the zip64 extra field). Depending on process/heap layout, this can crash the tool (heap over-read past the mapping) or leak adjacent memory content into the derived interface jar's parsed filename/attribute data, corrupting the ijar output or exposing unrelated memory. It does not reduce to a bare parsing crash from malformed input alone; it is specifically an integrity-guarantee failure — the one bounds check that is supposed to keep the "central directory" access contained within the validated file extent is defeated by controlled 64-bit overflow.

### Likelihood Explanation
Exploitability requires only crafting a Zip64 archive with adversarial EOCD/Zip64-EOCD values, which is straightforward to construct offline (no interaction with the victim beyond having them build against the malicious artifact). It does not require defeating any hash/integrity check — the attacker is the legitimate publisher of the artifact bytes, so the archive's checksum matches by construction, and this is exactly the "hostile origin server/mirror" scenario contemplated: the integrity mechanism verifies the bytes are what the attacker published, not that they are a well-formed/safe zip. This makes the bug directly reachable by any unprivileged party who can get a victim to build a `java_import` (or similar ijar-consuming) target against an artifact they control.

### Recommendation
Fix the bounds check in `FindZipCentralDirectory` to avoid overflow, e.g. by validating each operand individually and using subtraction against `in_length` (`central_dir_offset > in_length || central_dir_size > in_length - central_dir_offset`) rather than adding two attacker-controlled 64-bit values. Additionally, add explicit bounds checks in `ProcessCentralDirEntry` (mirroring `EnsureRemaining()` used in `ProcessLocalFileEntry`) before reading fixed-size fields and before `memcpy`ing `file_name_length`/`extra_field_length`/`data_size` bytes, so that a corrupted or adversarial central-directory pointer cannot cause reads past the mapped input file regardless of how it was derived.

### Proof of Concept
A reproducible test would be added as a `googletest` case (or extend `src/tools/singlejar`/`third_party/ijar` test suite) that:
1. Constructs a minimal ZIP file containing a valid local file entry, followed by a crafted End-Of-Central-Directory record and Zip64 End-Of-Central-Directory record where `central_dir_offset = UINT64_MAX - 8` and `central_dir_size = 16` (so the sum overflows to `7`, which is `<= in_length`).
2. Calls `FindZipCentralDirectory` (or the higher-level `InputZipFile::Open`/`ProcessNext` entry point used by the `ijar`/`zipper` binaries) on this crafted buffer.
3. Asserts that the function either rejects the archive (expected, post-fix) or, pre-fix, that `*central_dir` computed from `end_of_central_dir - central_dir_size` points outside `[bytes, bytes + in_length)`, and that subsequent iteration via `ProcessCentralDirEntry` performs reads outside the input buffer (detectable under ASan as heap-buffer-overflow).

I was unable to fully trace every downstream consumer of the resulting `*offset` output within `zip.cc`/`zip_main.cc` in this session (e.g., all call sites of `FindZipCentralDirectory` and how `*offset` feeds into `ijar`'s stripping logic elsewhere in the file) due to iteration limits; a full PoC/ASan run in a Devin session would be needed to confirm the exact crash signature and whether the out-of-bounds read can be escalated beyond an over-read into corrupted output bytes.

### Citations

**File:** third_party/ijar/zip.cc (L497-530)
```text
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
```

**File:** third_party/ijar/zip.cc (L615-620)
```text
  u4 number_of_this_disk = get_u4be(current);
  u4 disk_with_central_dir = get_u4le(current);
  u8 central_dir_entries_on_this_disk = get_u8le(current);
  u8 central_dir_entries = get_u8le(current);
  u8 central_dir_size = get_u8le(current);
  u8 central_dir_offset = get_u8le(current);
```

**File:** third_party/ijar/zip.cc (L766-776)
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
```
