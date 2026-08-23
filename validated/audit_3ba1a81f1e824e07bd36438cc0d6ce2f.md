## Analysis

The manifest is fetched from remote object storage (S3, or another Gitaly's `LocalStorage`) and parsed with `mvcc.ParseManifest`, which is invoked on every RPC that prepares an MVCC cache (`LocalCache.prepare` → `prepareManifestFile` → `ParseManifest`) [1](#0-0) . The manifest content is untrusted binary data coming from a remote/object-storage source that a repository's writers (ordinary users performing MVCC writes/pushes) populate via `Session.Publish`, so its `TOC` offsets are attacker-influenceable in the same trust boundary as a crafted push.

In `parseChunks`, the 8-byte, unsigned 64-bit TOC offsets are decoded and immediately narrowed to Go's signed `int` with no range check prior to the cast:

```go
chunkOffsetInFile := int(binary.BigEndian.Uint64(data[chunkOffset+4 : chunkOffset+tocChunkSize]))
...
nextChunkOffsetInFile := int(binary.BigEndian.Uint64(data[nextChunkStart+4 : nextChunkStart+tocChunkSize]))

if chunkOffsetInFile > dataEnd || nextChunkOffsetInFile > dataEnd || nextChunkOffsetInFile < chunkOffsetInFile {
    return nil, fmt.Errorf("chunk %d has out-of-bounds offsets [%d, %d)", i, chunkOffsetInFile, nextChunkOffsetInFile)
}
chunkSize := nextChunkOffsetInFile - chunkOffsetInFile
``` [2](#0-1) 

This is a structurally identical bug class to the Sherlock finding: an unsigned 64-bit value larger than `math.MaxInt64` (e.g. `0x8000000000000000`) wraps to a negative `int` when cast, exactly as `uint256 pointsPerShare` wrapped to a negative `int256`. The subsequent bounds/sanity checks (`> dataEnd`, `< chunkOffsetInFile`) are written assuming non-negative values and are silently defeated once the cast produces a negative number, letting a corrupted/crafted chunk offset/size slip past validation.

### Title
Unsafe uint64→int cast on MVCC manifest chunk offsets bypasses bounds validation - (File: internal/git/mvcc/manifest.go)

### Summary
`parseChunks` in the MVCC manifest parser casts attacker-influenceable 64-bit chunk offsets straight from `binary.BigEndian.Uint64` to Go's signed `int` without first checking they fit in the positive range of `int`, mirroring the reported "unsafe cast" pattern that turns a large unsigned value into a negative signed value and corrupts subsequent arithmetic/validation.

### Finding Description
`ParseManifest` is called on manifest bytes fetched from remote/durable object storage every time an MVCC-backed RPC prepares its git command environment [3](#0-2) . `parseChunks` decodes each 12-byte TOC entry's 8-byte offset field as `uint64` and immediately truncates/casts it to `int`:

```go
chunkOffsetInFile := int(binary.BigEndian.Uint64(data[chunkOffset+4 : chunkOffset+tocChunkSize]))
nextChunkOffsetInFile := int(binary.BigEndian.Uint64(data[nextChunkStart+4 : nextChunkStart+tocChunkSize]))
``` [4](#0-3) 

If an offset value exceeds `math.MaxInt64` (i.e., has its high bit set), the cast to `int` (64-bit signed on all supported Gitaly platforms) yields a negative number — the exact "unsafe cast" failure mode described in the report, where a value that overflows the destination's signed range silently flips sign instead of erroring. The validation immediately after,

```go
if chunkOffsetInFile > dataEnd || nextChunkOffsetInFile > dataEnd || nextChunkOffsetInFile < chunkOffsetInFile {
    return nil, fmt.Errorf(...)
}
chunkSize := nextChunkOffsetInFile - chunkOffsetInFile
``` [5](#0-4) 

was written under the assumption that decoded offsets are non-negative. A negative `chunkOffsetInFile`/`nextChunkOffsetInFile` trivially satisfies `<= dataEnd`, and by choosing both offsets to be negative-but-ordered values an attacker can pass the `nextChunkOffsetInFile < chunkOffsetInFile` check too, producing an out-of-range `manifestChunk{offset: <negative>, size: ...}` that downstream slicing (`data[start:start+pw]` in the PATH-chunk loop, and OBJS/REFS offset arithmetic) uses without further range checks [6](#0-5) .

### Impact Explanation
A crafted manifest (written by any MVCC repository writer/session, or a compromised/malicious upstream storage entry synced through the normal push/publish flow) can produce negative chunk offsets that bypass the intended bounds checks. Downstream code then slices `data[start:start+pw]` and similar expressions using these corrupted offsets, which will either panic (index out of range → RPC-handler crash / denial of service for the process handling MVCC-backed reads and writes) or, depending on how Go's slice-bounds panics are recovered elsewhere, could allow parsing garbage/attacker-chosen byte ranges as valid manifest paths, corrupting the resolved `Paths`, `objStart/objCount`, and `refStackOrder` used to decide which artifacts/reftables are treated as part of the repository's authoritative state.

### Likelihood Explanation
`ParseManifest` runs on every MVCC RPC that needs to prepare a session/environment (`LocalCache.prepare`), and the manifest content originates from data published through `Session.Publish`/`Commit` by ordinary writers using the MVCC backend, then read back by any subsequent RPC (including read-only ones) via `prepareManifestFile` [7](#0-6) . Since MVCC is documented as an in-progress feature ("this is likely a result of the vibe coding" per the design doc) with no independent authentication of manifest bytes beyond the trailing SHA-256 self-checksum (which an attacker controlling the manifest bytes can also recompute), any party able to write a manifest into the backing store can trigger this path.

### Recommendation
Validate that decoded 64-bit offset/size values fit within `[0, len(data)]` and within `int`'s positive range *before* casting, e.g. reject any `uint64` value greater than `math.MaxInt` (or simply greater than `len(data)`) prior to conversion, and perform all TOC arithmetic in `uint64` (or a checked/bounds-safe helper) until final validation against `dataEnd` is complete. Apply the same treatment to the `int(binary.BigEndian.Uint32(...))` casts for `objStart`/`objCount` and the `REFS` index decoding in the same file [8](#0-7) .

### Proof of Concept
1. Construct a manifest byte buffer with a valid header (`magic`, `version=1`, `numChunks=1`, `pathWidth`, `hashAlgo`).
2. In the TOC, set chunk 0's offset field (`data[chunkOffset+4:chunkOffset+12]`) to `0xFFFFFFFFFFFFFFFF` (or another value ≥ `2^63`) and the terminator/next entry's offset field to a smaller but still negative-when-cast value such that `nextChunkOffsetInFile < chunkOffsetInFile` still evaluates false after the cast.
3. Append the required trailing SHA-256 checksum over the crafted payload so `ParseManifest`'s checksum check passes.
4. Call `mvcc.ParseManifest(data)` — the cast in `parseChunks` (lines 302–307) converts the huge `uint64` to a negative `int`; the guard at lines 309–313 is bypassed because the comparisons operate on already-negative signed integers, and the code proceeds to compute `chunkSize` and later slice `data` using the corrupted offset, causing an out-of-range panic or corrupted parse result instead of the intended `fmt.Errorf("chunk %d has out-of-bounds offsets ...")` rejection.

### Citations

**File:** internal/git/mvcc/cache.go (L264-369)
```go
// or when the RPC is an accessor one. This function is not part of the
// Cache interface, and is mostly used for testing.
func (m *LocalCache) ManifestPath() string {
	return m.environments["GIT_MVCC_MANIFEST_PATH"]
}

// prepare resolves the manifest pointer, then downloads every reftable it
// references from durable storage into the local cache directory.
func (m *LocalCache) prepare(ctx context.Context, hash string) (err error) {
	// An empty hash means to fetch the latest pointer.
	if hash == latestManifestPointer {
		// Read the manifest pointer from the RemoteStorage
		rawPointer, err := m.rs.ReadManifestPointer(ctx, m.repositoryID)
		if err != nil {
			return fmt.Errorf("read manifest pointer: %w", err)
		}
		hash = rawPointer
	}

	manifest, err := m.prepareManifestFile(ctx, hash)
	if err != nil {
		return fmt.Errorf("prepare manifest file: %w", err)
	}

	// Warm the cache. We only care about reftables for now. Objects will
	// be taken care of at a later stage.
	err = m.prepareArtifacts(ctx, manifest.RefPaths())
	if err != nil {
		return fmt.Errorf("prepare artifacts: %w", err)
	}

	// Prepare environment variables
	if m.readOnly {
		m.environments["GIT_MVCC_MANIFEST"] = hash
	} else {
		// For mutator RPCs, we create a temp manifest pointer file that points to the manifest file
		tmpManifestFileDir := filepath.Join(m.runtimeDir, mvccCacheDir, m.repositoryID)
		err = os.MkdirAll(tmpManifestFileDir, 0o755)
		if err != nil {
			return fmt.Errorf("make manifest temp dir: %w", err)
		}

		tmpManifestFile, err := os.CreateTemp(tmpManifestFileDir, "manifest-*")
		if err != nil {
			return fmt.Errorf("create temp manifest file: %w", err)
		}
		defer func() { _ = tmpManifestFile.Close() }()
		if _, err := tmpManifestFile.WriteString(hash); err != nil {
			return fmt.Errorf("write temp manifest file: %w", err)
		}
		m.baseHash = hash
		m.baseManifest = manifest
		m.environments["GIT_MVCC_MANIFEST_PATH"] = tmpManifestFile.Name()
	}

	return nil
}

// prepareManifestFile will read the manifest file defined by `hash` and return its parsed form.
// If the manifest file already exist on the local filesystem, the file is read from the filesystem.
// Else, the manifest file is fetched from ObjectStorage and then created on the local filesystem for future use.
func (m *LocalCache) prepareManifestFile(ctx context.Context, hash string) (manifest *Manifest, err error) {
	// Construct the manifest path
	manifestFilePath := filepath.Join(m.storagePath, m.repositoryID, mvccCacheDir, "manifests", hash)

	// Verify if it already exists on the local filesystem or not
	existLocally, err := fileExist(manifestFilePath)
	if err != nil {
		return nil, fmt.Errorf("check if file exists: %w", err)
	}

	if !existLocally {
		// If it does not exist, read content from ObjectStorage
		manifestReader, err := m.rs.ReadManifest(ctx, m.repositoryID, hash)
		if err != nil {
			return nil, fmt.Errorf("read manifest file: %w", err)
		}

		defer func() {
			closeErr := manifestReader.Close()
			err = errors.Join(err, closeErr)
		}()

		// Create the manifest file on the local file system atomically. This is a protection against
		// potential concurrent requests trying to create the same manifest file.
		if err = createFileAtomic(ctx, manifestFilePath, manifestReader); err != nil {
			return nil, fmt.Errorf("write manifest: %w", err)
		}
	}

	// Now that it exist on disk, let's open the file.
	manifestFile, err := os.Open(manifestFilePath)
	if err != nil {
		return nil, fmt.Errorf("opening manifest %s: %w", manifestFilePath, err)
	}

	// Read the content in memory. These files should not be that large.
	// The Manifest parsing logic needs the whole file at once to parse it.
	manifestContent := bytes.Buffer{}
	_, err = manifestContent.ReadFrom(manifestFile)
	if err != nil {
		return nil, fmt.Errorf("reading manifest: %w", err)
	}

	// Return the parsed manifest
	return ParseManifest(manifestContent.Bytes())
```

**File:** internal/git/mvcc/manifest.go (L182-196)
```go
	numPath := pathChunk.size / pw
	m.Paths = make([]string, numPath)
	for i := range numPath {
		start := pathChunk.offset + i*pw
		record := data[start : start+pw]
		end := bytes.IndexByte(record, 0)
		if end == -1 {
			return nil, fmt.Errorf("PATH record %d is not NUL-terminated", i)
		}
		p := string(record[:end])
		if !isValidManifestPath(p) {
			return nil, fmt.Errorf("manifest contains invalid path %q", p)
		}
		m.Paths[i] = p
	}
```

**File:** internal/git/mvcc/manifest.go (L204-224)
```go
		m.objStart = int(binary.BigEndian.Uint32(data[objsChunk.offset : objsChunk.offset+4]))
		m.objCount = int(binary.BigEndian.Uint32(data[objsChunk.offset+4 : objsChunk.offset+8]))
		if m.objStart+m.objCount > len(m.Paths) {
			return nil, fmt.Errorf("OBJS range [%d, %d) out of bounds (have %d paths)", m.objStart, m.objStart+m.objCount, len(m.Paths))
		}
	}

	// Process REFS chunk if present.
	refsChunk, err := chunks.AtIndex(chunks.refsIdx)
	if err == nil {
		if refsChunk.size%4 != 0 {
			return nil, fmt.Errorf("REFS chunk size %d not divisible by 4", refsChunk.size)
		}
		numRefs := refsChunk.size / 4
		m.refStackOrder = make([]string, numRefs)
		for i := range numRefs {
			idx := binary.BigEndian.Uint32(data[refsChunk.offset+i*4 : refsChunk.offset+i*4+4])
			if int(idx) >= len(m.Paths) {
				return nil, fmt.Errorf("REFS index %d out of bounds (have %d paths)", idx, len(m.Paths))
			}
			m.refStackOrder[i] = m.Paths[idx]
```

**File:** internal/git/mvcc/manifest.go (L299-319)
```go
	for i := range numChunks {
		// The TOC starts after the header
		chunkOffset := headerSize + i*tocChunkSize
		chunkID := binary.BigEndian.Uint32(data[chunkOffset : chunkOffset+4])
		chunkOffsetInFile := int(binary.BigEndian.Uint64(data[chunkOffset+4 : chunkOffset+tocChunkSize]))

		// Calculate the size of the chunk
		nextChunkStart := headerSize + (i+1)*tocChunkSize
		nextChunkOffsetInFile := int(binary.BigEndian.Uint64(data[nextChunkStart+4 : nextChunkStart+tocChunkSize]))

		if chunkOffsetInFile > dataEnd || nextChunkOffsetInFile > dataEnd || nextChunkOffsetInFile < chunkOffsetInFile {
			return nil, fmt.Errorf("chunk %d has out-of-bounds offsets [%d, %d)", i, chunkOffsetInFile, nextChunkOffsetInFile)
		}

		chunkSize := nextChunkOffsetInFile - chunkOffsetInFile

		result.chunks[i] = manifestChunk{
			id:     chunkID,
			offset: chunkOffsetInFile,
			size:   chunkSize,
		}
```
