## Off-by-one in BEEFY future-block-voting equivocation check (`<` instead of `<=`) — File: `substrate/frame/beefy/src/equivocation.rs`

### Summary
The `FutureBlockVotingProof` branch of `check_equivocation_proof` in the BEEFY equivocation-report pallet uses a strict `<` comparison against the current block number to decide whether a submitted vote actually targets a future block. This is the exact bug class from the external report ("use `<` instead of `<=`"): a boundary value (the vote's `block_number` being *equal* to the current block) is treated as satisfying the "future" condition when it is not actually a future block, letting the proof pass validation when it shouldn't.

### Finding Description
In `substrate/frame/beefy/src/equivocation.rs`, the equivocation-proof check for the `FutureBlockVotingProof` variant is: [1](#0-0) 

```rust
EquivocationEvidenceFor::FutureBlockVotingProof(equivocation_proof, _) => {
    let FutureBlockVotingProof { vote } = equivocation_proof;
    // Check if the commitment actually targets a future block
    if vote.commitment.block_number < frame_system::Pallet::<T>::block_number() {
        return Err(Error::<T>::InvalidFutureBlockVotingProof);
    }
    ...
```

The intent, per the doc comment on `FutureBlockVotingProof`, is to prove that "an authority voted for a future block" [2](#0-1) . "Future" means `vote.commitment.block_number` must be strictly greater than the current chain height (`frame_system::Pallet::<T>::block_number()`), i.e. a block that has not happened yet. A vote whose `block_number` is *equal* to the current block number is not a future block — it is the block currently being processed/authored — and should be rejected as an invalid future-block-voting proof, exactly like Case 1 of the external report (`block.number <= batch.blockHeight` should be `block.number < batch.blockHeight` to correctly identify "not yet reached").

Here, the guard only rejects when `block_number < current`. When `block_number == current`, the guard is skipped and the code proceeds to accept the proof as valid future-block misbehavior evidence, then validates only the signature [3](#0-2)  before returning `Ok(())`, which leads to slashing via `process_evidence`.

This check is reachable from the public, unprivileged extrinsics `report_future_block_voting` (signed, reporter-paid) and `report_future_block_voting_unsigned` (validated via `ValidateUnsigned`), both of which route into `T::EquivocationReportSystem::process_evidence` → `check_equivocation_proof`: [4](#0-3) 

### Impact Explanation
`check_equivocation_proof` gates whether a slashable offence (`Perbill::from_percent(50)`) is reported against a BEEFY authority [5](#0-4) . Because the boundary condition is off by one, a vote whose target block number exactly equals the current block height at time of reporting is mis-accepted as proof of "future block voting" even though it targets the present, not the future. This can cause an honest validator's legitimate vote to be misclassified as equivocation and trigger an unjustified 50% slash and offence report — a runtime bug that compromises intended slashing behavior and can cause unwarranted loss of a validator's funds (an unprivileged reporter earns a fee-waived, valid-looking report against an honest party).

### Likelihood Explanation
Exploitation only requires an attacker to possess/observe any genuinely signed BEEFY vote message and submit the equivocation-report extrinsic in the exact block whose number matches the vote's `commitment.block_number`. No malicious validator, collator, governance, or leaked-key assumption is required — this is a public dispatch (`report_future_block_voting`/`_unsigned`) usable by any signed account or via unsigned validated extrinsic, matching the "public dispatch wrapper" and "proof verification" pivots called out in the task.

### Recommendation
Change the comparison to correctly identify non-future blocks, mirroring the external report's fix:
```rust
if vote.commitment.block_number <= frame_system::Pallet::<T>::block_number() {
    return Err(Error::<T>::InvalidFutureBlockVotingProof);
}
```
This ensures only strictly future block numbers (`> current`) are accepted as valid future-block-voting equivocation evidence.

### Proof of Concept
1. Let `N = frame_system::Pallet::<T>::block_number()` at the block in which the report extrinsic will be included.
2. Obtain (via gossip/observation) a genuine signed BEEFY `VoteMessage` from authority `A` whose `commitment.block_number == N`.
3. Submit `report_future_block_voting_unsigned(equivocation_proof = FutureBlockVotingProof { vote }, key_owner_proof)` in block `N`.
4. `vote.commitment.block_number < frame_system::Pallet::<T>::block_number()` evaluates `N < N == false`, so the guard is skipped; signature check passes; `check_equivocation_proof` returns `Ok(())`.
5. `process_evidence` reports the offence with `slash_fraction = 50%` against authority `A`, even though `A`'s vote did not target an actual future block.

### Citations

**File:** substrate/frame/beefy/src/equivocation.rs (L274-288)
```rust
			EquivocationEvidenceFor::FutureBlockVotingProof(equivocation_proof, _) => {
				let FutureBlockVotingProof { vote } = equivocation_proof;
				// Check if the commitment actually targets a future block
				if vote.commitment.block_number < frame_system::Pallet::<T>::block_number() {
					return Err(Error::<T>::InvalidFutureBlockVotingProof);
				}

				let is_signature_valid =
					check_commitment_signature(&vote.commitment, &vote.id, &vote.signature);
				if !is_signature_valid {
					return Err(Error::<T>::InvalidForkVotingProof);
				}

				Ok(())
			},
```

**File:** substrate/frame/beefy/src/equivocation.rs (L292-298)
```rust
	fn slash_fraction(&self) -> Option<Perbill> {
		match self {
			EquivocationEvidenceFor::DoubleVotingProof(_, _) => None,
			EquivocationEvidenceFor::ForkVotingProof(_, _) |
			EquivocationEvidenceFor::FutureBlockVotingProof(_, _) => Some(Perbill::from_percent(50)),
		}
	}
```

**File:** substrate/primitives/consensus/beefy/src/lib.rs (L431-436)
```rust
/// Proof showing that an authority voted for a future block.
#[derive(Clone, Debug, Decode, DecodeWithMemTracking, Encode, PartialEq, TypeInfo)]
pub struct FutureBlockVotingProof<Number, Id: RuntimeAppPublic> {
	/// The equivocated vote.
	pub vote: VoteMessage<Number, Id, Id::Signature>,
}
```

**File:** substrate/frame/beefy/src/lib.rs (L359-383)
```rust
		/// Report future block voting equivocation. This method will verify the equivocation proof
		/// and validate the given key ownership proof against the extracted offender.
		/// If both are valid, the offence will be reported.
		#[pallet::call_index(5)]
		#[pallet::weight(T::WeightInfo::report_future_block_voting(
			key_owner_proof.validator_count(),
			T::MaxNominators::get(),
		))]
		pub fn report_future_block_voting(
			origin: OriginFor<T>,
			equivocation_proof: Box<FutureBlockVotingProof<BlockNumberFor<T>, T::BeefyId>>,
			key_owner_proof: T::KeyOwnerProof,
		) -> DispatchResultWithPostInfo {
			let reporter = ensure_signed(origin)?;

			T::EquivocationReportSystem::process_evidence(
				Some(reporter),
				EquivocationEvidenceFor::FutureBlockVotingProof(
					*equivocation_proof,
					key_owner_proof,
				),
			)?;
			// Waive the fee since the report is valid and beneficial
			Ok(Pays::No.into())
		}
```
