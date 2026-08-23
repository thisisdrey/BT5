### Title
Unsafe `uint64`→`int` Downcast Before Bounds Validation in MVCC Manifest Chunk Parsing - ([File: internal/git/mvcc/manifest.go])

### Summary
`parseChunks` in the Gitaly MVCC (Multi-Version Concurrency Control) reference-backend manifest parser converts attacker/storage-controlled 64-bit chunk offsets to Go's signed `int` *before* validating them against the buffer length, mirroring the Uniswap `int56`→`int24` downcast-before-division bug class: an unsafe narrowing/sign conversion happens before the value is checked, allowing a value that should be rejected to silently pass validation.

### Finding Description
`ParseManifest` reads a binary "chunk" format (à la `gitformat-chunk(5)`) whose table-of-contents entries encode each chunk's absolute byte offset as a big-endian `uint64`. In `parseChunks`, this value is downcast to `int` immediately upon reading, and only afterward compared against `dataEnd`: [1](#0-0) 

Because the conversion `int(binary.BigEndian.Uint64(...))` happens before the `chunkOffsetInFile > dataEnd` bounds check, a manifest whose TOC entry encodes a `uint64` value with the sign bit set (i.e., `>= 1<<63`) wraps around to a negative `int` (Go's `int` is 64-bit on the platforms Gitaly targets, so this is a two's-complement wraparound rather than a narrowing truncation, but the same "cast-before-validate" defect class as the reported issue). A negative `chunkOffsetInFile`/`nextChunkOffsetInFile` can satisfy `chunkOffsetInFile > dataEnd` as `false` and `nextChunkOffsetInFile < chunkOffsetInFile` as `false` depending on the crafted values, letting a malformed chunk descriptor through the validation gate that was specifically designed to catch out-of-bounds offsets. The resulting negative `offset`/`size` are then propagated into `manifestChunk` and used later for `data[start:start+pw]` slicing in `ParseManifest`'s PATH/OBJS/REFS processing: [2](#0-1) [3](#0-2) 

A negative slice index/length triggers a Go runtime panic ("slice bounds out of range"), rather than a silent out-of-bounds read (Go's runtime bounds-checks slices), so the class of impact differs from the original Solidity report (which caused silent wrong data) — here it manifests as an unhandled panic.

This parser is invoked from `LocalCache.prepareManifestFile`/`putNewArtifacts`, which is exercised on essentially every RPC against an MVCC-backend repository via `LocalCache.prepare`, since it resolves the manifest pointer and downloads/parses the manifest before Git commands run: [4](#0-3) [5](#0-4) 

The manifest bytes are fetched from a pluggable `RemoteStorage` backend (`ReadManifest`) keyed by a content hash; the only integrity check performed is that the manifest's own trailing SHA-256 checksum matches its own payload (a self-consistency check, not an authentication of the value against the caller's expected hash), so any entity able to place a byte sequence under the expected manifest key in the storage backend can supply arbitrary, self-consistent (but structurally malicious) chunk offsets.

### Impact Explanation
A malformed/corrupted manifest object with a crafted TOC offset can bypass the bounds check meant to catch invalid offsets and later trigger a runtime panic during manifest parsing. Since parsing occurs in the request path for both accessor and mutator RPCs on MVCC-backend repositories (`LocalCache.prepare`), an unrecovered panic in this path can crash the RPC-handling goroutine (or, if not recovered by gRPC middleware, the whole `gitaly` process), denying service for that repository (or worse, for the whole node, depending on panic recovery configuration). This qualifies as a "DoS of a handler" per the accepted impact classes.

### Likelihood Explanation
Exploitation requires the ability to make Gitaly fetch/parse a manifest whose bytes were not produced by Gitaly's own legitimate manifest writer (e.g., a compromised/malicious remote storage backend, a corrupted artifact, or a future code path that accepts externally supplied manifests). This is a real, concrete code-level defect (cast-before-validate) reachable through the standard MVCC request-preparation flow, but its likelihood is gated by how manifest bytes reach the storage tier — it is not directly settable by an ordinary user via git push/fetch content today, only via write access to the manifest storage/keying scheme. It is a genuine and directly attributable bug in the validation logic, independent of the exact threat model that supplies the bytes.

### Recommendation
Perform bounds validation on the raw `uint64` values *before* converting them to `int`, and reject any offset that exceeds `math.MaxInt` or is not itself less than or equal to `dataEnd` while still a `uint64`:

```go
rawOffset := binary.BigEndian.Uint64(data[chunkOffset+4 : chunkOffset+tocChunkSize])
if rawOffset > uint64(dataEnd) {
    return nil, fmt.Errorf("chunk %d has out-of-bounds offset %d", i, rawOffset)
}
chunkOffsetInFile := int(rawOffset)
```
Apply the same pattern to `nextChunkOffsetInFile`, and add an explicit `chunkOffsetInFile < 0 || nextChunkOffsetInFile < 0` guard as defense in depth. Additionally, recover from panics around manifest parsing so that a malformed manifest degrades to an RPC error instead of crashing the handler/process.

### Proof of Concept
1. Craft a manifest body with a valid header/TOC-terminator/PATH chunk, but set one TOC entry's 8-byte offset field to `0x8000000000000000` (a `uint64` with the sign bit set).
2. Recompute the trailing SHA-256 trailer over the payload so `ParseManifest`'s checksum check passes (this is trivial since the attacker controls the entire payload).
3. Place this file where `LocalCache.prepareManifestFile` will read it (e.g., under the manifest storage key referenced by a repository's manifest pointer).
4. Invoke any RPC on the corresponding MVCC-backend repository; `parseChunks` computes `chunkOffsetInFile := int(0x8000000000000000)`, which becomes negative; the `chunkOffsetInFile > dataEnd` check is bypassed, and later slicing (`data[start:start+pw]`) with a negative `start` panics, crashing the handling goroutine/process instead of returning a clean parse error. [6](#0-5)

### Citations

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

**File:** internal/git/mvcc/manifest.go (L198-209)
```go
	// Process OBJS chunk if present.
	objsChunk, err := chunks.AtIndex(chunks.objsIdx)
	if err == nil {
		if objsChunk.size != 8 {
			return nil, fmt.Errorf("OBJS chunk has unexpected size %d (want 8)", objsChunk.size)
		}
		m.objStart = int(binary.BigEndian.Uint32(data[objsChunk.offset : objsChunk.offset+4]))
		m.objCount = int(binary.BigEndian.Uint32(data[objsChunk.offset+4 : objsChunk.offset+8]))
		if m.objStart+m.objCount > len(m.Paths) {
			return nil, fmt.Errorf("OBJS range [%d, %d) out of bounds (have %d paths)", m.objStart, m.objStart+m.objCount, len(m.Paths))
		}
	}
```

**File:** internal/git/mvcc/manifest.go (L299-313)
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
```

**File:** internal/git/mvcc/cache.go (L270-320)
```go
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
```

**File:** internal/git/mvcc/cache.go (L325-370)
```go
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
}
```

**File:** internal/git/mvcc/manifest_test.go (L380-398)
```go
// validManifest returns a well-formed body with PATH, OBJS and REFS chunks that
// ParseManifest accepts. Individual tests mutate it to exercise one failure.
func validManifest(t *testing.T) []byte {
	t.Helper()

	const width = 16
	paths := []string{
		"pack/aaaa.idx",
		"pack/aaaa.pack",
		"pack/aaaa.rev",
		"refs/bbbb.ref",
		"refs/cccc.ref",
	}
	return buildManifest(t, manifestVersion, width, hashAlgoSHA1, []chunkSpec{
		{chunkIDPath, pathChunkData(width, paths...)},
		{chunkIDObjs, objsChunkData(0, 3)},
		{chunkIDRefs, refsChunkData(4, 3)},
	})
}
```
