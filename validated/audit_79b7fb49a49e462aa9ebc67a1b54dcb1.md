Audit Report

## Title
Zero-weight public `submit_commitment` extrinsic allows underpriced signature-verification work to stall block production - (File: `bridges/modules/beefy/src/lib.rs`)

## Summary
`pallet::submit_commitment` is declared with `#[pallet::weight(0)]` [1](#0-0)  yet internally performs O(N) expensive cryptographic verification work proportional to the configured BEEFY authority-set length via `utils::verify_signatures` [2](#0-1)  and `utils::get_authorities_mmr_root` [3](#0-2) , both invoked from `utils::verify_commitment` [4](#0-3) . Any signed account can trigger this unbounded-cost path for free from the runtime's weight-accounting perspective.

## Finding Description
The extrinsic is callable by any signed account via `ensure_signed(origin)?` [5](#0-4) . The `MaxRequests` bound is checked immediately after that, but it only throttles *successful* commitment imports — `RequestCount` is only incremented after `verify_commitment` succeeds [6](#0-5) , so any number of failing calls (bad/missing signatures) still pass the `RequestCount < MaxRequests` gate and reach the expensive verification path. Inside `verify_signatures`, the loop iterates through the whole authority set performing a full signature-verification call per entry until either enough valid signatures are found or the set is exhausted [7](#0-6) . An attacker who knows the current authority-set length (public via `CurrentAuthoritySetInfo`) can submit a commitment with `signatures.len() == authority_set_info.len` but mostly `None`/invalid entries, which passes the early length/id checks in `verify_commitment` [8](#0-7)  and only fails after the full scan. Because the call's declared weight is `0`, none of this computation is charged to the block-weight limit or the submitter's fee, unlike the pallet's other calls which use `T::DbWeight::get().reads_writes(...)` [9](#0-8) .

## Impact Explanation
This matches the allowed impact category "public underpriced work that degrades block production or stalls bridge processing." An unprivileged, signed account can pack a block with many `submit_commitment` calls that fail only after doing full O(N) authority-set signature verification and MMR-root recomputation, consuming real CPU time disproportionate to the zero weight accounted by the runtime, degrading block-production throughput for any chain that integrates this pallet.

## Likelihood Explanation
High for any deployment of this pallet: no privileged role, no compromised keys, and no malicious relayer/validator/collator assumption is required — only a signed account paying the near-zero fee implied by weight `0`, plus knowledge of the public `CurrentAuthoritySetInfo` length. The condition is fully attacker-controlled and repeatable across blocks. Note, however, that within this repository the `pallet_bridge_beefy` module is not currently wired into any `construct_runtime!` instantiation found in this codebase [10](#0-9) , so the vulnerability is latent in the pallet's public code but requires a consuming runtime to include it for live exploitation.

## Recommendation
Replace `#[pallet::weight(0)]` on `submit_commitment` with a weight function proportional to `validator_set.len()` (benchmarked per-signature and per-authority MMR-root cost), consistent with the `T::DbWeight::get().reads_writes(...)` pattern used elsewhere in the pallet. Additionally, consider bounding/capping the accepted `validator_set` length and rejecting oversized or excessively sparse signature sets fail-fast, before the verification loop runs.

## Proof of Concept
1. Read `CurrentAuthoritySetInfo` (public storage) to learn the current authority-set `len`.
2. Craft a `BridgedBeefySignedCommitment` with `signatures.len() == authority_set_info.len`, matching `commitment.validator_set_id`, but with mostly `None`/invalid signature entries, so it passes the early checks in `verify_commitment` and only fails in `verify_signatures` after scanning the whole authority set.
3. Submit many such `submit_commitment` transactions within a single block; since declared weight is `0`, the runtime's block-weight accounting does not restrict inclusion, forcing the block author to perform O(N) signature-verification work per transaction across many transactions "for free," degrading real block-production throughput.

### Citations

**File:** bridges/modules/beefy/src/lib.rs (L96-128)
```rust
#[frame_support::pallet(dev_mode)]
pub mod pallet {
	use super::*;
	use bp_runtime::{BasicOperatingMode, OwnedBridgeModule};
	use frame_support::pallet_prelude::*;
	use frame_system::pallet_prelude::*;

	#[pallet::config]
	pub trait Config<I: 'static = ()>: frame_system::Config {
		/// The upper bound on the number of requests allowed by the pallet.
		///
		/// A request refers to an action which writes a header to storage.
		///
		/// Once this bound is reached the pallet will reject all commitments
		/// until the request count has decreased.
		#[pallet::constant]
		type MaxRequests: Get<u32>;

		/// Maximal number of imported commitments to keep in the storage.
		///
		/// The setting is there to prevent growing the on-chain state indefinitely. Note
		/// the setting does not relate to block numbers - we will simply keep as much items
		/// in the storage, so it doesn't guarantee any fixed timeframe for imported commitments.
		#[pallet::constant]
		type CommitmentsToKeep: Get<u32>;

		/// The chain we are bridging to here.
		type BridgedChain: ChainWithBeefy;
	}

	#[pallet::pallet]
	#[pallet::without_storage_info]
	pub struct Pallet<T, I = ()>(PhantomData<(T, I)>);
```

**File:** bridges/modules/beefy/src/lib.rs (L155-182)
```rust
		#[pallet::weight((T::DbWeight::get().reads_writes(2, 3), DispatchClass::Operational))]
		pub fn initialize(
			origin: OriginFor<T>,
			init_data: InitializationDataOf<T, I>,
		) -> DispatchResult {
			Self::ensure_owner_or_root(origin)?;

			let is_initialized = <ImportedCommitmentsInfo<T, I>>::exists();
			ensure!(!is_initialized, <Error<T, I>>::AlreadyInitialized);

			tracing::info!(target: LOG_TARGET, ?init_data, "Initializing bridge BEEFY pallet");
			Ok(initialize::<T, I>(init_data)?)
		}

		/// Change `PalletOwner`.
		///
		/// May only be called either by root, or by `PalletOwner`.
		#[pallet::call_index(1)]
		#[pallet::weight((T::DbWeight::get().reads_writes(1, 1), DispatchClass::Operational))]
		pub fn set_owner(origin: OriginFor<T>, new_owner: Option<T::AccountId>) -> DispatchResult {
			<Self as OwnedBridgeModule<_>>::set_owner(origin, new_owner)
		}

		/// Halt or resume all pallet operations.
		///
		/// May only be called either by root, or by `PalletOwner`.
		#[pallet::call_index(2)]
		#[pallet::weight((T::DbWeight::get().reads_writes(1, 1), DispatchClass::Operational))]
```

**File:** bridges/modules/beefy/src/lib.rs (L198-200)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(0)]
		pub fn submit_commitment(
```

**File:** bridges/modules/beefy/src/lib.rs (L210-211)
```rust
			Self::ensure_not_halted().map_err(Error::<T, I>::BridgeModule)?;
			ensure_signed(origin)?;
```

**File:** bridges/modules/beefy/src/lib.rs (L213-233)
```rust
			ensure!(Self::request_count() < T::MaxRequests::get(), <Error<T, I>>::TooManyRequests);

			// Ensure that the commitment is for a better block.
			let commitments_info =
				ImportedCommitmentsInfo::<T, I>::get().ok_or(Error::<T, I>::NotInitialized)?;
			ensure!(
				commitment.commitment.block_number > commitments_info.best_block_number,
				Error::<T, I>::OldCommitment
			);

			// Verify commitment and mmr leaf.
			let current_authority_set_info = CurrentAuthoritySetInfo::<T, I>::get();
			let mmr_root = utils::verify_commitment::<T, I>(
				&commitment,
				&current_authority_set_info,
				&validator_set,
			)?;
			utils::verify_beefy_mmr_leaf::<T, I>(&mmr_leaf, mmr_proof, mmr_root)?;

			// Update request count.
			RequestCount::<T, I>::mutate(|count| *count += 1);
```

**File:** bridges/modules/beefy/src/utils.rs (L17-31)
```rust
/// Get the MMR root for a collection of validators.
pub(crate) fn get_authorities_mmr_root<
	'a,
	T: Config<I>,
	I: 'static,
	V: Iterator<Item = &'a BridgedBeefyAuthorityId<T, I>>,
>(
	authorities: V,
) -> BridgedMmrHash<T, I> {
	let merkle_leafs = authorities
		.cloned()
		.map(BridgedBeefyAuthorityIdToMerkleLeaf::<T, I>::convert)
		.collect::<Vec<_>>();
	merkle_root::<BridgedMmrHashing<T, I>, _>(merkle_leafs)
}
```

**File:** bridges/modules/beefy/src/utils.rs (L59-94)
```rust
fn verify_signatures<T: Config<I>, I: 'static>(
	commitment: &BridgedBeefySignedCommitment<T, I>,
	authority_set: &BridgedBeefyAuthoritySet<T, I>,
) -> Result<(), Error<T, I>> {
	ensure!(
		commitment.signatures.len() == authority_set.len(),
		Error::<T, I>::InvalidCommitmentSignaturesLen
	);

	// Ensure that the commitment was signed by enough authorities.
	let msg = commitment.commitment.encode();
	let mut missing_signatures = signatures_required(authority_set.len());
	for (idx, (authority, maybe_sig)) in
		authority_set.validators().iter().zip(commitment.signatures.iter()).enumerate()
	{
		if let Some(sig) = maybe_sig {
			if authority.verify(sig, &msg) {
				missing_signatures = missing_signatures.saturating_sub(1);
				if missing_signatures == 0 {
					break;
				}
			} else {
				tracing::debug!(
					target: LOG_TARGET,
					%idx,
					?authority,
					?sig,
					"Signed commitment contains incorrect signature of validator"
				);
			}
		}
	}
	ensure!(missing_signatures == 0, Error::<T, I>::NotEnoughCorrectSignatures);

	Ok(())
}
```

**File:** bridges/modules/beefy/src/utils.rs (L107-126)
```rust
pub(crate) fn verify_commitment<T: Config<I>, I: 'static>(
	commitment: &BridgedBeefySignedCommitment<T, I>,
	authority_set_info: &BridgedBeefyAuthoritySetInfo<T, I>,
	authority_set: &BridgedBeefyAuthoritySet<T, I>,
) -> Result<BridgedMmrHash<T, I>, Error<T, I>> {
	// Ensure that the commitment is signed by the best known BEEFY validator set.
	ensure!(
		commitment.commitment.validator_set_id == authority_set_info.id,
		Error::<T, I>::InvalidCommitmentValidatorSetId
	);
	ensure!(
		commitment.signatures.len() == authority_set_info.len as usize,
		Error::<T, I>::InvalidCommitmentSignaturesLen
	);

	verify_authority_set(authority_set_info, authority_set)?;
	verify_signatures(commitment, authority_set)?;

	extract_mmr_root(commitment)
}
```
