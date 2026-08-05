### Title
Fixed 8-bit length heuristic in `BackedCandidate::validator_indices_and_core_index` misinterprets validator vote bits as an injected `CoreIndex` for large backing groups - (`polkadot/primitives/src/v9/mod.rs`)

### Summary
`BackedCandidate` packs the backing-group validator bitfield and an optional 8-bit `CoreIndex` into a single `BitVec`, similarly to how `QuantAMMWeightedPool` packs weights and multipliers into fixed-size storage slots. Just like the QuantAMM bug, the *split point* used to separate the two logical fields is derived from a fixed constant (`8`) rather than from an explicit, carried flag indicating whether a `CoreIndex` was actually injected. When the backing-group size is not a small, elastic-scaling-only value, this fixed-offset heuristic silently misclassifies genuine validator vote bits as a `CoreIndex`, and truncates the validator bitfield used for signature verification.

### Finding Description
`BackedCandidate::validator_indices` is a `BitVec<u8, Lsb0>` that, when `CoreIndex` injection is used, is extended by exactly 8 bits holding the core index (`inject_core_index`): [1](#0-0) 

Whether those extra 8 bits are present is not tracked by any explicit flag stored alongside the field. Instead, `validator_indices_and_core_index` infers presence purely from the *total bit length* being larger than 8: [2](#0-1) 

```rust
let core_idx_offset = self.validator_indices.len().saturating_sub(8);
if core_idx_offset > 0 {
    let (validator_indices_slice, core_idx_slice) =
        self.validator_indices.split_at(core_idx_offset);
    return (validator_indices_slice, Some(CoreIndex(core_idx_slice.load::<u8>() as u32)));
}
(&self.validator_indices, None)
```

This is structurally the same class of bug as the QuantAMM report: a packed structure is split using a constant offset (`4` tokens there, `8` bits here) without regard to whether the *actual* number of "extra" elements present matches the assumption baked into the offset arithmetic.

`set_validator_indices_and_core_index` makes injection *conditional* — the core index is appended only `if let Some(core_index) = maybe_core_index`: [3](#0-2) 

So the wire/storage format legitimately has two shapes:
- No core index injected: `validator_indices.len() == group_size` (plain votes bitfield).
- Core index injected: `validator_indices.len() == group_size + 8`.

`validator_indices_and_core_index` cannot distinguish these two shapes when `group_size > 8` and no core index was injected: `core_idx_offset = group_size - 8 > 0`, so the function unconditionally assumes the trailing 8 bits are a `CoreIndex` and returns only the first `group_size - 8` bits as validator votes. This drops 8 genuine validator-vote bits from the bitfield handed to backing-signature verification, and manufactures a bogus `CoreIndex` out of validator vote bits that were never intended to encode a core index.

Backing groups with more than 8 members are an entirely normal, non-malicious configuration (group size = `num_validators / num_cores`, which is large whenever cores are few relative to the validator set, e.g. on chains not using elastic scaling / single-core scheduling). No malicious validator, collator, or governance action is required to reach this state — it is a direct consequence of ordinary group sizing combined with the optional (`Option<CoreIndex>`) injection path.

### Impact Explanation
The mis-split bitfield is consumed by the runtime's backing verification path (`polkadot/runtime/parachains/src/inclusion/mod.rs`, `paras_inherent/mod.rs`), which uses `validator_indices_and_core_index` to determine (a) which validators in the assigned group are considered to have backed the candidate and (b) which specific `CoreIndex` the candidate commits to. Both are load-bearing correctness invariants: the reported backing votes must bind exactly to the validators who signed, and the committed core index must bind exactly to what the group scheduling logic assigned. A length-based misclassification breaks both bindings — this matches the report's "proof/message must bind ... exactly once" pivot, since the parachain backing check is effectively a local proof-acceptance mechanism binding validity votes to validator identities and to a core.

### Likelihood Explanation
This triggers under ordinary (non-adversarial) network parameterization whenever `group_size > 8` and core-index injection is not exercised (i.e., legacy/non-elastic-scaling candidates on such chains), so no privileged or malicious actor is required — it is a latent logic defect in a field-length heuristic, directly analogous to the QuantAMM index-threshold bug.

### Recommendation
Do not infer the presence of an injected `CoreIndex` from bit length. Instead, either:
- store an explicit `Option<CoreIndex>` (or a presence flag) alongside `validator_indices` in the encoded `BackedCandidate`, or
- carry the intended group size explicitly (e.g., from the assigned group at the time of construction) and only strip the trailing 8 bits when the caller can prove core-index injection actually happened for that specific candidate, rather than deriving it opportunistically from `len() > 8`.

### Proof of Concept
Conceptual reproduction (matches the pattern of the QuantAMM PoC, which fed correctly-shaped data through a mis-shaped split routine):
1. Construct a `BackedCandidate` via `set_validator_indices_and_core_index(bits, None)` where `bits.len() == 12` (a backing group of 12 validators, no elastic scaling, matching real deployments with few cores).
2. Call `validator_indices_and_core_index()`.
3. Observe: `core_idx_offset = 12 - 8 = 4 > 0`, so the function returns only the first 4 bits as the validator-votes bitfield (discarding 8 real vote bits) and synthesizes `Some(CoreIndex(...))` from bits that are actually validator votes, even though no `CoreIndex` was ever injected (`maybe_core_index == None`).

### Citations

**File:** polkadot/primitives/src/v9/mod.rs (L3088-3101)
```rust
	/// Get a copy of the validator indices and the assumed core index, if any.
	pub fn validator_indices_and_core_index(
		&self,
	) -> (&BitSlice<u8, bitvec::order::Lsb0>, Option<CoreIndex>) {
		// `BackedCandidate::validity_indices` are extended to store a 8 bit core index.
		let core_idx_offset = self.validator_indices.len().saturating_sub(8);
		if core_idx_offset > 0 {
			let (validator_indices_slice, core_idx_slice) =
				self.validator_indices.split_at(core_idx_offset);
			return (validator_indices_slice, Some(CoreIndex(core_idx_slice.load::<u8>() as u32)));
		}

		(&self.validator_indices, None)
	}
```

**File:** polkadot/primitives/src/v9/mod.rs (L3103-3108)
```rust
	/// Inject a core index in the validator_indices bitvec.
	fn inject_core_index(&mut self, core_index: CoreIndex) {
		let core_index_to_inject: BitVec<u8, bitvec::order::Lsb0> =
			BitVec::from_vec(vec![core_index.0 as u8]);
		self.validator_indices.extend(core_index_to_inject);
	}
```

**File:** polkadot/primitives/src/v9/mod.rs (L3110-3121)
```rust
	/// Update the validator indices and core index in the candidate.
	pub fn set_validator_indices_and_core_index(
		&mut self,
		new_indices: BitVec<u8, bitvec::order::Lsb0>,
		maybe_core_index: Option<CoreIndex>,
	) {
		self.validator_indices = new_indices;

		if let Some(core_index) = maybe_core_index {
			self.inject_core_index(core_index);
		}
	}
```
