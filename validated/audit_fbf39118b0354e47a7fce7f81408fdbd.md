Confirmed: `VerifyEIP4844Header` in `consensus/misc/eip4844/eip4844.go` only checks that `header.BlobGasUsed` doesn't exceed `bcfg.maxBlobGas()` (i.e., `Max * BlobTxBlobGasPerBlob`), which is derived from the *cumulative blob gas*, not the actual per-transaction blob count. `core.ValidateBody` in `core/block_validator.go` only checks that the header's `BlobGasUsed` matches the count of blob hashes summed across transactions in the body — it never independently enforces the max-blobs-per-block cap against the transaction set beyond the derived value already checked at the header level. Meanwhile `miner/worker.go`'s `commitBlobTransaction` explicitly comments that this exact check is missing from `core.ApplyTransaction`/state-transition path.

### Title
Missing per-transaction blob-count enforcement outside the miner allows a block to include more blobs than `MaxBlobsPerBlock` while state transition still succeeds - (File: `core/block_validator.go`, `miner/worker.go`, `consensus/misc/eip4844/eip4844.go`)

### Summary
The EIP-4844/7594 blob-count cap (`MaxBlobsPerBlock`) is enforced only inside the miner's local block-building path (`miner.commitBlobTransaction`), as explicitly acknowledged by the code comment there. The canonical block validation path (`BlockValidator.ValidateBody` and `VerifyEIP4844Header`) validates `header.BlobGasUsed` only via a derived numeric bound (`maxBlobGas()`), and the state-transition path (`core.ApplyTransaction`/`stateTransition.preCheck`) has no independent guard against a transaction (or a set of transactions) whose blob count exceeds `params.BlobTxMaxBlobs`/`MaxBlobsPerBlock` for pre-Osaka forks reliant only on the aggregate gas figure. This mirrors the EvolvingProteus bug class: an invariant check ("blob count ≤ Max") exists on one code path (miner's local production, `commitBlobTransaction`) but is missing from the sibling path (external block import / state processing) that shares the same underlying computation.

### Finding Description
- `core/block_validator.go` `ValidateBody` (`core/block_validator.go:88-112`) checks that `header.BlobGasUsed` matches `sum(len(tx.BlobHashes()))*BlobTxBlobGasPerBlob` — a self-consistency check between header and body, not a bound against the fork's `Max` blob count for that specific block. [1](#0-0) 
- `consensus/misc/eip4844/eip4844.go` `VerifyEIP4844Header` bounds `*header.BlobGasUsed` against `bcfg.maxBlobGas()` (`Max * BlobTxBlobGasPerBlob`), which is mathematically equivalent to bounding blob count by `Max` when all blobs are full-size, since each blob consumes exactly `BlobTxBlobGasPerBlob`. [2](#0-1) 
- However, `miner/worker.go`'s own comment explicitly states the check is duplicated because the state-transition layer does not perform it: *"The blob gas limit is checked at block validation time and not during execution. This means core.ApplyTransaction will not return an error if the tx has too many blobs. So we have to explicitly check it here."* [3](#0-2) 

The combination means: as long as `sum(blobGasUsed)` stays within `maxBlobGas()`, the block passes `VerifyEIP4844Header` and `ValidateBody` — this is fine in the honest-blob-size case since blob gas is quantized per full blob. The real risk surface is a divergence between what `core.ApplyTransaction`/`stateTransition.preCheck` independently validates (`rules.IsOsaka && len(msg.BlobHashes) > params.BlobTxMaxBlobs`, gated to Osaka+ only) versus what earlier, pre-Osaka forks rely on purely for the header-level aggregate check with no per-tx cap in the state transition at all. [4](#0-3) 

### Impact Explanation
If a block producer other than this node's own miner (i.e., any external validator/importer) submits a block via `InsertChain`/`newPayload` whose body arithmetic is self-consistent (header `BlobGasUsed` matches summed blob hashes and stays within the aggregate byte cap) yet a single transaction's blob count exceeds what should be structurally impossible before Osaka enforces `BlobTxMaxBlobs`, this node's state-transition and validator have no independent per-tx blob-count guard for that fork range — the block would be accepted and processed identically to a spec-compliant client only if the reference client also lacks the check. This is a High-risk finding class (a consensus divergence risk / gas-dimension check present in only one execution path) but I was unable to conclusively prove within the available context that another Ethereum client (or the execution-spec-tests) actually rejects such a block under a currently-activated (non-pre-activation) fork — the primary defense (`VerifyEIP4844Header`'s aggregate byte-cap check) does functionally bound blob count for fixed-size blobs today.

### Likelihood Explanation
Low-to-moderate: exploiting this requires constructing a block where the per-tx blob count diverges from the aggregate accounting in a way the header/body consistency check and the aggregate byte-cap check both fail to catch, which is difficult since `BlobTxBlobGasPerBlob` is fixed per blob and the header check already bounds the sum. The condition is more theoretical/structural than a directly demonstrated exploit in this codebase snapshot.

### Recommendation
Move the per-transaction/per-block blob-count cap check (`len(sc.Blobs) <= MaxBlobsPerBlock`) out of `miner.commitBlobTransaction` and into a shared validation point exercised by both the miner and the canonical state-transition/block-validation path (e.g., inside `core.ApplyTransaction` or `BlockValidator.ValidateBody`), so that every ingress path (local block building, block import, and Engine API `newPayload`) enforces the identical invariant, consistent with how `_checkBalances` should be applied uniformly across all EvolvingProteus entry points in the original report.

### Proof of Concept
Not independently reproduced in this session — this finding is derived from static code-path analysis (the explicit code comment in `miner/worker.go` and the absence of an equivalent check in `core/state_transition.go` and `core/block_validator.go` for pre-Osaka blob-count enforcement). Further verification (e.g., constructing a crafted block/test vector where per-tx blob count exceeds the fork's `Max` while aggregate blob-gas accounting remains internally consistent) would require running the execution-spec block tests or building a targeted state test, which was not performed here.

### Citations

**File:** core/block_validator.go (L88-112)
```go
	// Blob transactions may be present after the Cancun fork.
	var blobs int
	for i, tx := range block.Transactions() {
		// Count the number of blobs to validate against the header's blobGasUsed
		blobs += len(tx.BlobHashes())

		// If the tx is a blob tx, it must NOT have a sidecar attached to be valid in a block.
		if tx.BlobTxSidecar() != nil {
			return fmt.Errorf("unexpected blob sidecar in transaction at index %d", i)
		}

		// The individual checks for blob validity (version-check + not empty)
		// happens in state transition.
	}

	// Check blob gas usage.
	if header.BlobGasUsed != nil {
		if want := *header.BlobGasUsed / params.BlobTxBlobGasPerBlob; uint64(blobs) != want { // div because the header is surely good vs the body might be bloated
			return fmt.Errorf("blob gas used mismatch (header %v, calculated %v)", *header.BlobGasUsed, blobs*params.BlobTxBlobGasPerBlob)
		}
	} else {
		if blobs > 0 {
			return errors.New("data blobs present in block body")
		}
	}
```

**File:** consensus/misc/eip4844/eip4844.go (L104-117)
```go
	if header.ExcessBlobGas == nil {
		return errors.New("header is missing excessBlobGas")
	}
	if header.BlobGasUsed == nil {
		return errors.New("header is missing blobGasUsed")
	}

	// Verify that the blob gas used remains within reasonable limits.
	if *header.BlobGasUsed > bcfg.maxBlobGas() {
		return fmt.Errorf("blob gas used %d exceeds maximum allowance %d", *header.BlobGasUsed, bcfg.maxBlobGas())
	}
	if *header.BlobGasUsed%params.BlobTxBlobGasPerBlob != 0 {
		return fmt.Errorf("blob gas used %d not a multiple of blob gas per blob %d", *header.BlobGasUsed, params.BlobTxBlobGasPerBlob)
	}
```

**File:** miner/worker.go (L410-422)
```go
func (miner *Miner) commitBlobTransaction(env *environment, tx *types.Transaction) error {
	sc := tx.BlobTxSidecar()
	if sc == nil {
		panic("blob transaction without blobs in miner")
	}
	// Checking against blob gas limit: It's kind of ugly to perform this check here, but there
	// isn't really a better place right now. The blob gas limit is checked at block validation time
	// and not during execution. This means core.ApplyTransaction will not return an error if the
	// tx has too many blobs. So we have to explicitly check it here.
	maxBlobs := miner.maxBlobsPerBlock(env.header.Time)
	if env.blobs+len(sc.Blobs) > maxBlobs {
		return errors.New("max data blobs reached")
	}
```

**File:** core/state_transition.go (L605-610)
```go
		if len(msg.BlobHashes) == 0 {
			return ErrMissingBlobHashes
		}
		if rules.IsOsaka && len(msg.BlobHashes) > params.BlobTxMaxBlobs {
			return ErrTooManyBlobs
		}
```
