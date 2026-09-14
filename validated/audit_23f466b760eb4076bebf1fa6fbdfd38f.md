### Title
Element size truncation in `encodeNodeCompressed`/`decodeNodeCompressed` corrupts trie-node state history - (File: triedb/pathdb/nodes.go)

### Summary
`encodeNodeCompressed` serializes trie-node diff elements (child hashes / short-node keys or values) into a byte stream, prefixing each element with a single length byte. `decodeNodeCompressed` reads that single byte back as the element's size. If an element's actual length exceeds 255 bytes, the length prefix silently wraps (`byte(len(element))`), so encode and decode disagree about how many bytes belong to the element.

### Finding Description
In `encodeNodeCompressed`, each diff element is length-prefixed with exactly one byte: [1](#0-0) 
The comment "1 byte is sufficient for element size" assumes elements never exceed 255 bytes, but nothing in `NodeDifference` or its callers enforces that bound — a modified short-node value (e.g., the RLP-encoded value stored at a trie leaf, including account/storage node blobs during a diff-mode history encoding) can legitimately be larger than 255 bytes.

The producing call site is in `encodeNodeHistory`, which builds diffs from `trie.NodeDifference(oldvalue, n.Blob)` and feeds them straight into `encodeNodeCompressed` without any size check: [2](#0-1) 

On the decode side, `decodeNodeCompressed` trusts the single length byte to slice the buffer: [3](#0-2) 

If `len(element) > 255`, the encoder writes `byte(len(element))` (i.e. `len(element) mod 256`), then appends the *full* element bytes. The decoder, reading that truncated size byte, will slice out fewer bytes than were actually written for this element, leaving the remaining true bytes of that element to be misinterpreted as the start of the *next* element's size byte and content, or to be reported as unexpected trailing bytes. This desynchronizes every subsequent element in the record.

### Impact Explanation
This code path is part of the path-based state history / trie-node journal used by `triedb/pathdb` to record diffs for later reconstruction (used during rollbacks/history reads of trie nodes). A corrupted history record means the reconstructed trie node for a given path/owner will not match the node value that was actually persisted at the time the block was executed — i.e., persisted state history diverges from the executed state. Depending on how the corrupted diff is later applied (e.g., during unwind/rollback of historical state), this can silently produce a wrong trie node value, which propagates into a wrong state root during history-based reconstruction, or an outright decode error causing an unrecoverable failure of history reads on an otherwise valid history entry.

### Likelihood Explanation
This requires a trie node whose modified short-node value (or embedded child content passed through the diff path) exceeds 255 bytes in a single state transition, while also landing in "diff mode" rather than the full-value fallback (`encodeFullValue`'s probabilistic sampling, or forced full-encode for the first two account-trie levels). Such large values are plausible for RLP-encoded contract account/storage entries or embedded nodes carrying big values, but the exact conditions under which `NodeDifference` returns oversized elements and diff-mode is chosen (rather than fallback to `encodeNodeFull`) would need further tracing through `trie.NodeDifference` to confirm concretely reachable inputs from a single valid block. That part could not be fully verified from the available context.

### Recommendation
Widen the length prefix (e.g., use a variable-length or 2+ byte size field, or clamp/guard by falling back to `encodeNodeFull` whenever `len(element) > 255`) in `encodeNodeCompressed`/`decodeNodeCompressed`, and add an explicit bounds check before encoding to prevent silent truncation.

### Proof of Concept
Not fully constructible from static analysis alone: reproducing this concretely requires confirming that `trie.NodeDifference` can return an element (short-node value or full-node child content) larger than 255 bytes for a single valid state transition while diff-mode (`encodeFullValue` returning false) is selected — that data-flow into `trie.NodeDifference`'s output size bound was not verified within the available index/tool budget.

### Citations

**File:** triedb/pathdb/nodes.go (L508-511)
```go
	for _, element := range elements {
		enc = append(enc, byte(len(element))) // 1 byte is sufficient for element size
		enc = append(enc, element...)
	}
```

**File:** triedb/pathdb/nodes.go (L570-591)
```go
		// Read element size
		size := int(data[0])
		data = data[1:]

		// Check if we have enough data for the element
		if len(data) < size {
			return nil, nil, fmt.Errorf("invalid data: expected %d bytes, got %d", size, len(data))
		}
		// Extract element
		if size == 0 {
			elements = append(elements, nil)

			// The zero-size element is practically unexpected, for node deletion
			// the rlp.EmptyString is still expected. Log loudly for the potential
			// programming error.
			log.Error("Empty element from compressed node, please open an issue", "raw", data)
		} else {
			element := make([]byte, size)
			copy(element, data[:size])
			data = data[size:]
			elements = append(elements, element)
		}
```

**File:** triedb/pathdb/nodes.go (L674-682)
```go
				nElem, indices, diffs, err := trie.NodeDifference(oldvalue, n.Blob)
				if err != nil {
					encodeFull = true // fallback to the full node encoding
				} else {
					// Encode the node difference as the history element
					addExt := nElem != 2 // fullNode
					blob := encodeNodeCompressed(addExt, diffs, indices)
					nodes[owner][path] = blob
				}
```
