Based on my research, I found a concrete analog in the reftable parsing code.

### Title
Out-of-bounds slice panic when parsing attacker-crafted reftable ref blocks - (File: internal/git/reftable/reftable.go)

### Summary
`Table.parseRefBlock` and `Table.getRefsFromBlock` in [1](#0-0)  decode variable-length, attacker-influenced length fields (`prefixLength`, `suffixLength`, hash sizes, symref sizes) read directly out of a reftable's raw bytes and use them to slice a shared `[]byte` buffer without validating the resulting indices against the buffer bounds, analogous to the reported class of "convert an oversized/untrusted numeric value into a narrower type/size and use it unchecked," which crashes the process instead of returning an error.

### Finding Description
`getBlockRange`, `extractBlockLen`, `getVarInt`, and `getRefsFromBlock` compute offsets (`idx`, `prefixLength`, `suffixLength`, `hashSize`) straight from bytes in the reftable file: [2](#0-1) 
These derived, unvalidated sizes are then used directly to slice `src`: [3](#0-2) 
Similarly, `parseRefBlock` computes `blockStart+currentBS-2` (a `uint` subtraction that can underflow to a huge value if `currentBS < 2`) and slices `src` at that offset before reading `RestartCount`: [1](#0-0) 
None of `prefixLength`, `suffixLength`, `hashSize`, or `currentBS` are checked against the length of `src` or against `b.RestartStart`/`b.FullBlockSize` before use, so a crafted reftable block can drive `idx+suffixLength`, `blockStart+currentBS-2`, or the underflowed subtraction well past `len(src)`, triggering a Go slice-bounds-out-of-range **panic** rather than a decode error — the same "attacker-controlled numeric value used unchecked in a size/offset computation, causing a process crash" pattern as the reported Rust `try_into().unwrap()` bug, just manifesting as a Go runtime panic instead of an `unwrap()` panic.

`ParseTable`/`GetReferences` do validate the header, footer, magic, version, and CRC32 checksum of the file [4](#0-3) , but the checksum only guarantees the bytes weren't corrupted in transit — it does not prevent a deliberately crafted, internally-consistent (correct CRC32) reftable file from containing malicious block/ref-record length fields.

### Impact Explanation
`Table.GetReferences` is invoked in production code from `reftableRecorder.stageTables`, which parses reftable files during transaction commit via `reftable.ParseTable(...)`: [5](#0-4)  and is reached from `TransactionManager.processTransaction` whenever the reference backend is reftables: [6](#0-5) . If a reftable file with attacker-influenced block/record contents can enter a repository's `reftable/` directory (e.g. via a crafted repository ingested by `CreateRepositoryFromBundle`, `CreateRepositoryFromURL`, `CreateFork`, replication, or import flows that clone/copy repository state before Gitaly's own Git binary re-writes the tables), a subsequent transaction touching that repository would crash the Gitaly process handling the RPC — a denial-of-service impacting all repositories served by that Gitaly node/partition, not just the attacker's own repository.

### Likelihood Explanation
I could not fully confirm from the index whether Gitaly ever parses a reftable file that was written by something other than Gitaly's own embedded Git binary (i.e., whether an externally supplied/untrusted reftable byte stream can reach `ParseTable`/`GetReferences` before Git re-normalizes it). All discovered production call sites (`reftableRecorder.stageTables`) operate on tables freshly written by Git during a transaction on the local, Gitaly-managed snapshot, which would generally be well-formed. Bundle/URL/fork/replication code paths clone or fetch via Git itself rather than writing raw reftable bytes directly, which would reduce reachability. Due to index size limits I was not able to inspect every ingestion path (e.g., `CreateRepositoryFromSnapshot`, `ReplicateRepository`, direct WAL log-entry file writes) to rule out a route where raw, attacker-controlled reftable bytes are written to disk and later parsed by this code without going through Git's own writer. I would recommend starting a Devin session with full repository access to confirm reachability before treating this as more than a defense-in-depth issue.

### Recommendation
Add explicit bounds checks in `getVarInt`, `getRefsFromBlock`, and `parseRefBlock` before every slice operation (`idx+suffixLength <= len(src)`, `idx+hashSize <= len(src)`, `currentBS >= 2` before subtracting, `blockStart+currentBS <= len(src)`), returning a descriptive error instead of allowing the runtime to panic on out-of-range indices. Recover-and-error at the RPC boundary as defense in depth is not a substitute for input validation here, since a panic during transaction commit could still leave partial state.

### Proof of Concept
Not independently reproducible from the index alone: it would require constructing a reftable file with a valid header/footer/CRC32 but a ref block whose `suffixLength` (via `getVarInt`) or `currentBS` (via `extractBlockLen`) is set larger than the remaining buffer, then invoking `Table.ParseTable` + `GetReferences()` on it (as exercised structurally, but only with valid data, by `TestParseTable`/`TestParseTable_validation` in [7](#0-6) ) to confirm a runtime panic instead of a returned error.

### Citations

**File:** internal/git/reftable/reftable.go (L76-142)
```go
// parseHeader parses the header of a reftable. reader should be at the beginning
// of the header.
func parseHeader(reader io.Reader, hdr *header) error {
	if err := binary.Read(reader, binary.BigEndian, &hdr.headerV1); err != nil {
		return fmt.Errorf("reading header: %w", err)
	}

	if hdr.Magic != magic {
		return fmt.Errorf("unexpected magic bytes: %q", hdr.Magic)
	}

	if !(hdr.Version == 1 || hdr.Version == 2) {
		return fmt.Errorf("unsupported version: %d", hdr.Version)
	}

	if hdr.Version == 2 {
		if err := binary.Read(reader, binary.BigEndian, &hdr.HashID); err != nil {
			return fmt.Errorf("read hash id: %w", err)
		}

		if !(hdr.HashID == hashIDSHA1 || hdr.HashID == hashIDSHA256) {
			return fmt.Errorf("unsupported hash id: %q", hdr.HashID)
		}
	}

	return nil
}

// footerEnd is the exact byte layout of the unique fields in the footer after the duplicated header.
type footerEnd struct {
	RefIndexOffset     uint64
	ObjectOffsetAndLen uint64
	ObjectIndexOffset  uint64
	LogOffset          uint64
	LogIndexPosition   uint64
	CRC32              uint32
}

// footer is the exact byte layout of a footer in a reftable.
type footer struct {
	header
	footerEnd
}

// parseFooter parses the footer of a reftable. reader should be at the beginning
// of the footer.
func parseFooter(reader io.Reader, f *footer) error {
	footerBytes, err := io.ReadAll(reader)
	if err != nil {
		return fmt.Errorf("read all: %w", err)
	}

	footerReader := bytes.NewReader(footerBytes)
	if err := parseHeader(footerReader, &f.header); err != nil {
		return fmt.Errorf("parse header: %w", err)
	}

	if err := binary.Read(footerReader, binary.BigEndian, &f.footerEnd); err != nil {
		return fmt.Errorf("parse remainder: %w", err)
	}

	if crc32.ChecksumIEEE(footerBytes[:len(footerBytes)-binary.Size(f.CRC32)]) != f.CRC32 {
		return errors.New("checksum mismatch")
	}

	return nil
}
```

**File:** internal/git/reftable/reftable.go (L198-219)
```go
// extractBlockLen extracts the block length from a given location.
func (t *Table) extractBlockLen(src []byte, blockStart uint) uint {
	return uint(big.NewInt(0).SetBytes(src[blockStart+1 : blockStart+4]).Uint64())
}

// getVarInt parses a variable int and increases the index.
func (t *Table) getVarInt(src []byte, start uint, blockEnd uint) (uint, uint, error) {
	var val uint

	val = uint(src[start]) & 0x7f

	for (uint(src[start]) & 0x80) > 0 {
		start++
		if start > blockEnd {
			return 0, 0, fmt.Errorf("exceeded block length")
		}

		val = ((val + 1) << 7) | (uint(src[start]) & 0x7f)
	}

	return start + 1, val, nil
}
```

**File:** internal/git/reftable/reftable.go (L244-292)
```go
		extra := (suffixLength & 0x7)
		suffixLength >>= 3

		refname := prefix[:prefixLength] + string(src[idx:idx+suffixLength])
		idx = idx + suffixLength

		idx, updateIndexDelta, err = t.getVarInt(src, idx, b.FullBlockSize)
		if err != nil {
			return nil, fmt.Errorf("getting update index delta: %w", err)
		}
		// we don't use this for now
		_ = updateIndexDelta

		reference := git.Reference{
			Name: git.ReferenceName(refname),
		}

		switch extra {
		case 0:
			// Deletion, no value
			reference.Target = t.shaFormat().ZeroOID.String()
		case 1:
			// Regular reference
			hashSize := t.shaFormat().Hash().Size()
			reference.Target = git.ObjectID(hex.EncodeToString(src[idx : idx+uint(hashSize)])).String()

			idx += uint(hashSize)
		case 2:
			// Peeled Tag
			hashSize := t.shaFormat().Hash().Size()
			reference.Target = git.ObjectID(hex.EncodeToString(src[idx : idx+uint(hashSize)])).String()

			idx += uint(hashSize)

			// For now we don't need the peeledOID, but we still need
			// to skip the index.
			// peeledOID := ObjectID(bytesToHex(t.src[idx : idx+uint(hashSize)]))
			idx += uint(hashSize)
		case 3:
			// Symref
			var size uint
			idx, size, err = t.getVarInt(src, idx, b.FullBlockSize)
			if err != nil {
				return nil, fmt.Errorf("getting symref size: %w", err)
			}

			reference.Target = git.ReferenceName(src[idx : idx+size]).String()
			reference.IsSymbolic = true
			idx = idx + size
```

**File:** internal/git/reftable/reftable.go (L303-327)
```go
// parseRefBlock parses a block and if it is a ref block, provides
// all the reference updates.
func (t *Table) parseRefBlock(src []byte, headerOffset, blockStart, blockEnd uint) ([]git.Reference, error) {
	currentBS := t.extractBlockLen(src, blockStart+headerOffset)

	fullBlockSize := t.blockSize
	if fullBlockSize == 0 {
		fullBlockSize = currentBS
	} else if currentBS < fullBlockSize && currentBS < (blockEnd-blockStart) && src[blockStart+currentBS] != 0 {
		fullBlockSize = currentBS
	}

	b := &block{
		BlockStart:    blockStart + headerOffset,
		FullBlockSize: fullBlockSize,
	}

	if err := binary.Read(bytes.NewBuffer(src[blockStart+currentBS-2:]), binary.BigEndian, &b.RestartCount); err != nil {
		return nil, fmt.Errorf("reading restart count: %w", err)
	}

	b.RestartStart = blockStart + currentBS - 2 - 3*uint(b.RestartCount)

	return t.getRefsFromBlock(src, b)
}
```

**File:** internal/gitaly/storage/storagemgr/partition/reftable.go (L93-97)
```go
		if err := func() (returnedErr error) {
			table, err := reftable.ParseTable(filepath.Join(r.snapshotRepoPath, "reftable", originalTableName.String()))
			if err != nil {
				return fmt.Errorf("parse table: %w", err)
			}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L1825-1833)
```go
				if refBackend == git.ReferenceBackendReftables || transaction.runHousekeeping != nil {
					if refBackend == git.ReferenceBackendReftables {
						if err := transaction.reftableRecorder.stageTables(ctx,
							mgr.getAbsolutePath(transaction.relativePath),
							transaction,
						); err != nil {
							return commitResult{error: fmt.Errorf("stage tables: %w", err)}
						}
					}
```

**File:** internal/git/reftable/reftable_test.go (L261-364)
```go
func TestParseTable_validation(t *testing.T) {
	if !testhelper.IsReftableEnabled() {
		t.Skip("This test is reftable specific.")
	}

	t.Parallel()

	ctx := testhelper.Context(t)
	cfg := testcfg.Build(t)

	patchHeader := func(t *testing.T, f *os.File, hdr header) {
		buf := bytes.NewBuffer(nil)
		require.NoError(t, binary.Write(buf, binary.BigEndian, hdr.headerV1))

		if hdr.headerV1.Version >= 2 {
			require.NoError(t, binary.Write(buf, binary.BigEndian, hdr.HashID))
		}

		_, err := f.WriteAt(buf.Bytes(), 0)
		require.NoError(t, err)
	}

	for _, tc := range []struct {
		desc                 string
		patchTable           func(*testing.T, *os.File, footer)
		expectedErrorMessage string
	}{
		{
			desc: "unexpected magic",
			patchTable: func(t *testing.T, file *os.File, f footer) {
				f.header.Magic = [...]byte{'I', 'V', 'A', 'L'}
				patchHeader(t, file, f.header)
			},
			expectedErrorMessage: `parse header: unexpected magic bytes: "IVAL"`,
		},
		{
			desc: "unsupported version",
			patchTable: func(t *testing.T, file *os.File, f footer) {
				f.header.Version = 3
				patchHeader(t, file, f.header)
			},
			expectedErrorMessage: `parse header: unsupported version: 3`,
		},
		{
			desc: "unsupported hash",
			patchTable: func(t *testing.T, file *os.File, f footer) {
				if f.Version < 2 {
					t.Skip("Hash ID is only present on reftable version 2.")
				}

				f.header.HashID = [...]byte{'I', 'V', 'A', 'L'}
				patchHeader(t, file, f.header)
			},
			expectedErrorMessage: `parse header: unsupported hash id: "IVAL"`,
		},
		{
			desc: "mismatching header and footer",
			patchTable: func(t *testing.T, file *os.File, f footer) {
				f.header.MaxUpdateIndex++
				patchHeader(t, file, f.header)
			},
			expectedErrorMessage: `footer doesn't match header`,
		},
		{
			desc: "invalid checksum",
			patchTable: func(t *testing.T, file *os.File, f footer) {
				// The checksum is at the end of the file. Modify the preceding byte without updating the
				// checksum to trigger a checksumming failure.
				info, err := file.Stat()
				require.NoError(t, err)

				_, err = file.WriteAt([]byte{255}, info.Size()-crc32.Size-1)
				require.NoError(t, err)
			},
			expectedErrorMessage: "parse footer: checksum mismatch",
		},
	} {
		t.Run(tc.desc, func(t *testing.T) {
			t.Parallel()

			_, repoPath := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{
				SkipCreationViaService: true,
			})

			tables, err := ReadTablesList(repoPath)
			require.NoError(t, err)

			tablePath := filepath.Join(repoPath, "reftable", tables[0].String())
			table, err := ParseTable(tablePath)
			require.NoError(t, err)
			defer testhelper.MustClose(t, table)

			file, err := os.OpenFile(tablePath, os.O_RDWR, 0)
			require.NoError(t, err)
			defer testhelper.MustClose(t, file)

			tc.patchTable(t, file, table.footer)

			table, err = ParseTable(tablePath)
			require.EqualError(t, err, tc.expectedErrorMessage)
			require.Nil(t, table)
		})
	}
}
```
