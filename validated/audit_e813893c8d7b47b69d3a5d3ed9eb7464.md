## Title
Gas/weight DoS in GRANDPA finality proof verification via quadratic-cost ancestry walk under linear weight charging - (File: `bridges/primitives/header-chain/src/justification/verification/mod.rs`)

### Summary
The `pallet-bridge-grandpa` `submit_finality_proof_ex` extrinsic charges weight as a **linear** function of `precommits.len()` (`p`) and `votes_ancestries.len()` (`v`) via `T::WeightInfo::submit_finality_proof(p, v)` [1](#0-0) . However, the actual verification loop in `strict`/`optimizer` verification calls `AncestryChain::ancestry()` once **per precommit**, and each call can walk the *entire* unvisited portion of the ancestry chain, giving true worst-case cost of **O(p·v)**, not O(p+v) as priced. This is the same broken invariant as OCL-1: an unbounded/incorrectly-priced iteration whose real cost scales super-linearly with attacker-controlled array sizes.

### Finding Description
`verify_justification` iterates over every precommit and, for each one whose authority/signature checks haven't short-circuited yet, calls `chain.ancestry(...)` **before** signature verification: [2](#0-1) 

`AncestryChain::ancestry()` walks parent pointers from the precommit's target hash back toward `base`, pushing every unvisited hash onto the route and only marking hashes as visited afterwards, via `mark_route_as_visited`, and **only if the vote is ultimately valid** (`process_valid_vote` path): [3](#0-2) [4](#0-3) 

Because the visited-cache is only populated on a **successful** vote, an attacker can craft a `GrandpaJustification` with:
- a long `votes_ancestries` chain of length `v` (headers `H_0 -> H_1 -> ... -> H_v` from `base`), and
- `p` precommits (up to `MAX_AUTHORITIES_COUNT` distinct authority ids, each with a syntactically valid-looking but ultimately **invalid signature**), each with `target_hash = H_v` (the tail of the chain).

Since the signature is checked *after* `ancestry()` is computed, every one of the `p` precommits forces a full `O(v)`-length BTreeMap walk before being rejected (via `process_invalid_signature_vote`) — the route is computed and discarded without ever being cached, because caching only happens on `process_valid_vote`. This yields `O(p·v)` real work.

The two call entry points (`submit_finality_proof` and `submit_finality_proof_ex`) charge weight solely from `T::WeightInfo::submit_finality_proof_weight(p, v)`, a benchmarked linear formula `base + coeff_p*p + coeff_v*v`: [5](#0-4) 

Neither `submit_finality_proof_ex` nor `submit_finality_proof_limits_extras` enforces a hard `ensure!` bound on `precommits.len()` or `votes_ancestries.len()` before dispatch — the latter only computes `is_weight_limit_exceeded` for later, informational fee-refund purposes, not as a rejection gate: [6](#0-5) 

So a relayer can submit an extrinsic whose declared/charged weight is linear in `p` and `v`, while its actual CPU cost is quadratic, exactly mirroring the OCL-1 pattern of "arrays iterated together, with all of one array iterated for each element of another," causing gas/weight underpricing.

### Impact Explanation
`submit_finality_proof`/`submit_finality_proof_ex` is a fully public, unsigned-origin-checked (`ensure_signed`) extrinsic with no privileged caller requirement — any relayer account can call it [7](#0-6) . By submitting maximally-sized, crafted justifications (long ancestry chains combined with many precommits targeting the tail of that chain with invalid signatures), an attacker can make actual block-execution time for a single extrinsic far exceed what its computed weight predicts. This degrades block production for the bridge-hub parachain and can stall processing of legitimate bridge finality updates — a "public underpriced work that degrades block production or stalls bridge processing" impact under the accepted impact gate.

### Likelihood Explanation
The attack requires only a signed account able to submit a normal transaction (no malicious relayer/validator/collator/governance assumption is needed beyond being a permissionless transaction sender), and the crafted justification does not need to be finality-valid — it only needs to pass initial size/shape checks before being rejected on signature verification, after already consuming disproportionate CPU. The core enabling gap — using only linear `p`/`v` terms in the weight formula while the underlying algorithm has an `O(p·v)` worst case, and the absence of a hard `ensure!` bound on `precommits.len()`/`votes_ancestries.len()` at call time — is directly visible in the code cited above.

### Recommendation
- Enforce a hard cap (`ensure!`) on `justification.commit.precommits.len()` (bounded to authority-set size) and `justification.votes_ancestries.len()` (bounded to `REASONABLE_HEADERS_IN_JUSTIFICATION_ANCESTRY` or similar) at the top of `submit_finality_proof_ex`, rejecting the call outright rather than only flagging it for reduced fee refund.
- Add a `p*v` cross term (or a tighter algorithmic bound) to the benchmarked weight formula so that worst-case ancestry-walk cost is always covered by the charged weight.
- Consider caching failed-lookup routes too (not just successful ones) or restructuring `ancestry()` to amortize repeated walks to the same target hash across precommits.

### Proof of Concept
1. Build a bridged-chain header chain `H_0 (base) -> H_1 -> ... -> H_v` and include all `v` headers in `justification.votes_ancestries`.
2. Construct `justification.commit.precommits` with `p` entries (`p` up to the configured authority-set size), each entry using a distinct authority id from the current voter set but an invalid/garbage `signature`, and each with `precommit.target_hash = H_v.hash()` (the deepest header).
3. Submit this justification via `submit_finality_proof_ex`. The transaction is accepted into the pool/block because `T::WeightInfo::submit_finality_proof_weight(p, v)` reports only linear-in-`p,v` weight.
4. During execution, `verify_justification`'s loop calls `chain.ancestry(H_v.hash(), ...)` for each of the `p` precommits; since the signature check fails afterwards, `mark_route_as_visited` is never invoked, so each of the `p` calls performs a fresh `O(v)` parent-hash walk — total `O(p·v)` operations — well beyond the weight that was charged for the block.

### Citations

**File:** bridges/modules/grandpa/src/lib.rs (L279-283)
```rust
		#[pallet::call_index(4)]
		#[pallet::weight(T::WeightInfo::submit_finality_proof_weight(
			justification.commit.precommits.len().saturated_into(),
			justification.votes_ancestries.len().saturated_into(),
		))]
```

**File:** bridges/modules/grandpa/src/lib.rs (L284-292)
```rust
		pub fn submit_finality_proof_ex(
			origin: OriginFor<T>,
			finality_target: Box<BridgedHeader<T, I>>,
			justification: GrandpaJustification<BridgedHeader<T, I>>,
			current_set_id: sp_consensus_grandpa::SetId,
			_is_free_execution_expected: bool,
		) -> DispatchResultWithPostInfo {
			Self::ensure_not_halted().map_err(Error::<T, I>::BridgeModule)?;
			ensure_signed(origin)?;
```

**File:** bridges/primitives/header-chain/src/justification/verification/mod.rs (L92-121)
```rust
	pub fn ancestry(
		&self,
		precommit_target_hash: &Header::Hash,
		precommit_target_number: &Header::Number,
	) -> Option<Vec<Header::Hash>> {
		if precommit_target_number < &self.base.number() {
			return None;
		}

		let mut route = vec![];
		let mut current_hash = *precommit_target_hash;
		loop {
			if current_hash == self.base.hash() {
				break;
			}

			current_hash = match self.parent_hash_of(&current_hash) {
				Some(parent_hash) => {
					let is_visited_before = self.unvisited.get(&current_hash).is_none();
					if is_visited_before {
						// If the current header has been visited in a previous call, it is a
						// descendent of `base` (we assume that the previous call was successful).
						return Some(route);
					}
					route.push(current_hash);

					*parent_hash
				},
				None => return None,
			};
```

**File:** bridges/primitives/header-chain/src/justification/verification/mod.rs (L288-321)
```rust

			// all precommits must be descendants of the target block
			let maybe_route =
				chain.ancestry(&signed.precommit.target_hash, &signed.precommit.target_number);
			if maybe_route.is_none() {
				let action = self
					.process_unrelated_ancestry_vote(precommit_idx)
					.map_err(Error::Precommit)?;
				if matches!(action, IterationFlow::Skip) {
					continue;
				}
			}

			// verify authority signature
			if !sp_consensus_grandpa::check_message_signature_with_buffer(
				&finality_grandpa::Message::Precommit(signed.precommit.clone()),
				&signed.id,
				&signed.signature,
				justification.round,
				context.authority_set_id,
				&mut signature_buffer,
			)
			.is_valid()
			{
				self.process_invalid_signature_vote(precommit_idx).map_err(Error::Precommit)?;
				continue;
			}

			// now we can count the vote since we know that it is valid
			self.process_valid_vote(signed);
			if let Some(route) = maybe_route {
				chain.mark_route_as_visited(route);
				cumulative_weight = cumulative_weight.saturating_add(authority_info.weight().get());
			}
```

**File:** bridges/modules/grandpa/src/weights.rs (L100-112)
```rust
	fn submit_finality_proof(p: u32, v: u32) -> Weight {
		// Proof Size summary in bytes:
		//  Measured:  `394 + p * (60 ±0)`
		//  Estimated: `4745`
		// Minimum execution time: 228_072 nanoseconds.
		Weight::from_parts(57_853_228, 4745)
			// Standard Error: 149_421
			.saturating_add(Weight::from_parts(36_708_702, 0).saturating_mul(p.into()))
			// Standard Error: 10_625
			.saturating_add(Weight::from_parts(1_469_032, 0).saturating_mul(v.into()))
			.saturating_add(T::DbWeight::get().reads(6_u64))
			.saturating_add(T::DbWeight::get().writes(6_u64))
	}
```

**File:** bridges/primitives/header-chain/src/lib.rs (L336-372)
```rust
pub fn submit_finality_proof_limits_extras<C: ChainWithGrandpa>(
	header: &C::Header,
	proof: &justification::GrandpaJustification<C::Header>,
) -> SubmitFinalityProofCallExtras {
	// the `submit_finality_proof` call will reject justifications with invalid, duplicate,
	// unknown and extra signatures. It'll also reject justifications with less than necessary
	// signatures. So we do not care about extra weight because of additional signatures here.
	let precommits_len = proof.commit.precommits.len().saturated_into();
	let required_precommits = precommits_len;

	// the weight check is simple - we assume that there are no more than the `limit`
	// headers in the ancestry proof
	let votes_ancestries_len: u32 = proof.votes_ancestries.len().saturated_into();
	let is_weight_limit_exceeded =
		votes_ancestries_len > C::REASONABLE_HEADERS_IN_JUSTIFICATION_ANCESTRY;

	// check if the `finality_target` is a mandatory header. If so, we are ready to refund larger
	// size
	let is_mandatory_finality_target =
		GrandpaConsensusLogReader::<BlockNumberOf<C>>::find_scheduled_change(header.digest())
			.is_some();

	// we can estimate extra call size easily, without any additional significant overhead
	let actual_call_size: u32 =
		header.encoded_size().saturating_add(proof.encoded_size()).saturated_into();
	let max_expected_call_size = max_expected_submit_finality_proof_arguments_size::<C>(
		is_mandatory_finality_target,
		required_precommits,
	);
	let extra_size = actual_call_size.saturating_sub(max_expected_call_size);

	SubmitFinalityProofCallExtras {
		is_weight_limit_exceeded,
		extra_size,
		is_mandatory_finality_target,
	}
}
```
