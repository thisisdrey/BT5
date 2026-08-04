## Analysis Result

The strongest local analog is the storage-weight-reclaim mechanism (`cumulus/pallets/weight-reclaim/src/lib.rs`, mirrored in `cumulus/primitives/storage-weight-reclaim/`). It reproduces the same broken invariant as the paymaster report: **a resource-consumption penalty that is discovered *after* the fee for that resource has already been fixed, and which is not charged back to the party who caused it.**

### Title
Storage-Weight-Reclaim silently socializes under-benchmarked PoV cost into `BlockWeight` instead of charging the submitter - (File: `cumulus/pallets/weight-reclaim/src/lib.rs`)

### Summary
`StorageWeightReclaim::post_dispatch_details` compares the *benchmarked* proof size (`accurate_weight`) used to compute the extrinsic's fee against the *measured* proof size reported by the host (`get_proof_size()`). When the measured proof size exceeds what the runtime charged for, the difference (`pov_size_missing_from_node`) is added directly into `frame_system::BlockWeight` to keep the node-side and runtime-side accounting consistent — but this excess is never reflected back into the fee paid by the extrinsic's sender. [1](#0-0) 

### Finding Description
This is structurally identical to the ERC-4337 paymaster bug: a cost that is realized *after* the fee-relevant quantity (`actualGasCost` / `accurate_weight`) has been computed is applied to a party (the paymaster / the block's remaining capacity) that has no mechanism to recover it from the party who caused it (the userOp sender / the extrinsic submitter).

- The fee actually charged to the submitter is computed from `accurate_weight`, which is `benchmarked_actual_weight` with its proof-size component overwritten by `measured_proof_size` — but only up to what the benchmark said was consumed: [2](#0-1) 
- Separately, `pov_size_missing_from_node` captures any measured PoV that exceeds what `BlockWeight` currently tracks, and is *added to block capacity accounting* rather than deducted from the submitter's refund or turned into an extra charge: [3](#0-2) 
- The final `accurate_unspent` refund computation subtracts `pov_size_missing_from_node` from the total, meaning the submitter's refund shrinks toward zero as PoV underestimation grows, but there is no path where they pay *more* than the pre-dispatch worst-case weight fee — so a call whose real PoV usage is chronically undercounted by its own weight annotation degrades the whole block's usable capacity every time it is included, at no marginal cost to the caller once the worst-case fee is already below what it would need to be to reflect true cost. [4](#0-3) 

The guard that exists (the `log::warn!`/`log::error!` diagnostics) is observability only — it does not reject the extrinsic, does not increase the fee, and does not throttle inclusion of further such extrinsics in the same block. [5](#0-4) 

### Impact Explanation
If a call type systematically under-declares its PoV/proof-size weight relative to what it actually consumes (a benchmarking gap, or a code path deliberately crafted to touch more trie nodes than the benchmark model accounts for), each inclusion of that call silently steals PoV budget from `BlockWeight` without the corresponding fee increase. Because `BlockWeight` is the authoritative gate for how much more can be included in the block, an attacker who repeatedly submits such calls can push the block's *tracked* usage artificially low relative to *real* PoV consumption, causing the block builder to over-pack blocks with underpriced work — degrading block production/import (nodes reprocessing an over-large PoV) without paying proportional fees. This matches the "public underpriced work that degrades block production" impact category.

### Likelihood Explanation
This requires no privileged actor: any unprivileged extrinsic submitter benefits automatically whenever their call's real proof-size cost exceeds its benchmarked estimate — a property of the call/weight function rather than of an attacker manipulating limits directly (unlike the ERC-4337 report where the user directly sets `callGasLimit`). This makes it more a systemic accounting gap than a directly attacker-triggerable exploit; a concrete instance would require a call whose PoV consumption is knowably and reproducibly larger than its declared weight (e.g., a call touching pathological trie depth/branching not represented in the benchmark's storage layout). I was not able to confirm, within the scope of this repo scan, a specific dispatchable whose weight annotation is provably and reproducibly wrong by a meaningful margin — this would need targeted benchmarking analysis against a concrete runtime's call weights, which is outside what static code reading can establish with certainty.

### Recommendation
When `pov_size_missing_from_node > 0`, either (a) reject the extrinsic post-dispatch by escalating to a hard error / reducing the block's remaining capacity by the *full* discrepancy and simultaneously charging the submitter a fee correction proportional to `pov_size_missing_from_node` (mirroring the report's recommendation to make the user, not the paymaster, responsible for the shortfall), or (b) cap the total number of extrinsics with unaccounted PoV overshoot allowed per block, so no single block can be pushed into oversized PoV purely through underpriced calls.

### Proof of Concept
Not independently reproducible from static analysis alone: constructing a concrete PoC requires identifying (or crafting) a dispatchable call whose benchmarked storage-proof weight is provably lower than its real trie-proof consumption under a specific storage topology, then observing that `pov_size_missing_from_node` is added to `BlockWeight` in `cumulus/pallets/weight-reclaim/src/lib.rs:247-259` while the submitter's fee (computed from `accurate_weight`, lines 216-233) does not increase to compensate. The existing unit test `test_incorporates_check_weight_unspent_weight_on_negative_reverse_order` in `cumulus/primitives/storage-weight-reclaim/src/tests.rs:561-602` exercises the refund-shrinking side of this mechanism and could be extended to assert that `BlockWeight` absorbs unpriced PoV without any corresponding fee delta.

### Citations

**File:** cumulus/pallets/weight-reclaim/src/lib.rs (L216-233)
```rust
		let benchmarked_actual_weight = post_info_with_inner.calc_actual_weight(info);

		let benchmarked_actual_proof_size = benchmarked_actual_weight.proof_size();
		if benchmarked_actual_proof_size < measured_proof_size {
			log::error!(
				target: LOG_TARGET,
				"Benchmarked storage weight smaller than consumed storage weight. \
				benchmarked: {benchmarked_actual_proof_size} consumed: {measured_proof_size}"
			);
		} else {
			log::trace!(
				target: LOG_TARGET,
				"Reclaiming storage weight. benchmarked: {benchmarked_actual_proof_size},
				consumed: {measured_proof_size}"
			);
		}

		let accurate_weight = benchmarked_actual_weight.set_proof_size(measured_proof_size);
```

**File:** cumulus/pallets/weight-reclaim/src/lib.rs (L235-262)
```rust
		let pov_size_missing_from_node = frame_system::BlockWeight::<T>::mutate(|current_weight| {
			let already_reclaimed = frame_system::ExtrinsicWeightReclaimed::<T>::get();
			current_weight.accrue(already_reclaimed, info.class);
			current_weight.reduce(info.total_weight(), info.class);
			current_weight.accrue(accurate_weight, info.class);

			// If we encounter a situation where the node-side proof size is already higher than
			// what we have in the runtime bookkeeping, we add the difference to the `BlockWeight`.
			// This prevents that the proof size grows faster than the runtime proof size.
			let block_size = frame_system::BlockSize::<T>::get().unwrap_or(0);
			let node_side_pov_size = proof_size_after_dispatch.saturating_add(block_size.into());
			let block_weight_proof_size = current_weight.total().proof_size();
			let pov_size_missing_from_node =
				node_side_pov_size.saturating_sub(block_weight_proof_size);
			if pov_size_missing_from_node > 0 {
				log::warn!(
					target: LOG_TARGET,
					"Node-side PoV size higher than runtime proof size weight. node-side: \
					{node_side_pov_size} block_size: {block_size} runtime: \
					{block_weight_proof_size}, missing: {pov_size_missing_from_node}. Setting to \
					node-side proof size."
				);
				current_weight
					.accrue(Weight::from_parts(0, pov_size_missing_from_node), info.class);
			}

			pov_size_missing_from_node
		});
```

**File:** cumulus/pallets/weight-reclaim/src/lib.rs (L264-277)
```rust
		// The saturation will happen if the pre-dispatch weight is underestimating the proof
		// size or if the node-side proof size is higher than expected.
		// In this case the extrinsic proof size weight reclaimed is 0 and not a negative reclaim.
		let accurate_unspent = info
			.total_weight()
			.saturating_sub(accurate_weight)
			.saturating_sub(Weight::from_parts(0, pov_size_missing_from_node));
		frame_system::ExtrinsicWeightReclaimed::<T>::put(accurate_unspent);

		// Call have already returned their unspent amount.
		// (also transaction extension prior in the pipeline, but there shouldn't be any.)
		let already_unspent_in_tx_ext_pipeline = post_info.calc_unspent(info);
		Ok(accurate_unspent.saturating_sub(already_unspent_in_tx_ext_pipeline))
	}
```
