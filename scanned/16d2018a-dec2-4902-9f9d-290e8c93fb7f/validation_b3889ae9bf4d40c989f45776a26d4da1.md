Confirmed: `permit` pallet at `substrate/frame/assets/precompiles/src/permit.rs` has no `#[pallet::call]` extrinsics at all — the only way to interact with it is through the `permit()` precompile entrypoint, which calls `use_permit` (verify + consume). There is no `revoke`, `invalidate`, or owner-callable function to bump `Nonces` proactively. The `Nonces` storage only advances as a side effect of a successful `use_permit` consumption.### Title
Owner cannot invalidate a leaked or mistaken EIP-2612 permit signature before it is consumed - ([File: substrate/frame/assets/precompiles/src/permit.rs])

### Summary
The `permit` pallet added for EIP-2612 gasless approvals stores per-`(verifying_contract, owner)` nonces used to bind and consume off-chain signed permit messages [1](#0-0) . The nonce only advances as a side effect of a successful `use_permit` call, and `use_permit`/`increment_nonce` are only ever invoked from inside the `permit()` precompile dispatcher [2](#0-1) . There is no dispatchable, precompile function, or any other public entrypoint that lets the `owner` proactively invalidate/cancel an outstanding permit signature they have already produced. This mirrors exactly the reported `PayrollManager` flaw: a nonce-bound, pre-signed off-chain artifact exists, but the protocol provides no mechanism to invalidate it before someone (anyone holding the signature) submits it on-chain.

### Finding Description
`permit.rs` defines the `Nonces` storage map and two verification paths:
- `do_verify_permit`, which checks deadline and recovers/validates the signer against `owner`, but does **not** touch the nonce [3](#0-2) .
- `use_permit`, which calls `do_verify_permit` and then calls `increment_nonce` to consume the permit [4](#0-3) .

`increment_nonce` is a plain internal pallet function, not gated by any `ensure_signed(origin) == owner` check, and more importantly there is no way to *call* it independently of consuming a valid signature [5](#0-4) . The pallet defines no `#[pallet::call]` section at all — the module only exposes helper functions used by the `permit()` precompile entrypoint in `lib.rs`, which is invoked exclusively via `IERC20::permitCall` decoding and immediately performs `use_permit` followed by the allowance update in one atomic transaction [6](#0-5) .

Consequently, once an owner signs an EIP-712 permit (e.g., approving a spender for a large `value`), that signature remains valid and executable by *anyone* who obtains it — through a leak, a compromised dApp frontend, a bug in the signing UI, or simply the owner changing their mind about the `spender`/`value`/`deadline` — until either the `deadline` passes or the permit is consumed. Unlike `frame_system::CheckNonce`, where a signer can invalidate a stale extrinsic by submitting any other transaction that bumps their account nonce [7](#0-6) , and unlike the `meta-tx` pallet, which explicitly relies on this same account-nonce bump to invalidate a stale meta-transaction (`meta_tx_extension_work` test demonstrates cancellation via `inc_account_nonce`) [8](#0-7) , the `permit` pallet's nonce is a **separate counter** keyed by `(verifying_contract, owner)` that is completely decoupled from the account's system nonce. The owner has no on-chain action available (no extrinsic, no precompile call) that increments this specific counter without simultaneously executing an approval they may not want.

### Impact Explanation
This directly maps to the "public underpriced work" / "unauthorized execution" pivot: an unprivileged third party who merely holds a previously produced signature can force execution of an approval the owner no longer wants in effect, and the owner has zero on-chain recourse to prevent it. Because `permit()` sets the ERC-20 allowance for the `spender` up to `value`, a stale or leaked permit can result in unauthorized token approvals being (re)established, which can then be exploited via `transferFrom` to move the owner's assets. The severity is bounded by `deadline`, but within that window the owner is fully exposed with no invalidation path — exactly the "unauthorized people being able to execute the transaction" concern raised in the external report.

### Likelihood Explanation
Likelihood is moderate: it requires either (a) permit signature leakage/frontend compromise, (b) an owner deciding a signed permit was made in error (wrong spender/value) before it is used, or (c) a relayer holding an old permit and choosing to submit it late. These are realistic real-world conditions for EIP-2612-style flows (this is a well-known practical issue with the EIP-2612 standard itself, and production integrations commonly add a permit-cancellation mechanism to mitigate it). No malicious validator, collator, governance actor, or leaked private key is required — only possession of a previously signed message, which is the exact "if it has already gone into the mempool, anyone can execute it" scenario called out in the source report.

### Recommendation
Add an explicit, owner-authenticated way to invalidate outstanding permits before consumption, analogous to the PayrollManager fix (PR 52) where any approver could invalidate the nonce. Concretely:
- Add a `#[pallet::call]` dispatchable (or a precompile function) such as `cancel_permit`/`invalidate_nonce` that lets `ensure_signed(origin)` (mapped to the corresponding `H160` owner via `AddressMapper`) increment `Nonces::<T>::get(verifying_contract, owner)` directly, bumping past any previously signed-but-unconsumed nonce.
- Alternatively, support EIP-2612-style nonce increments through a dedicated view/permit-revoke extension, or bind permits to a short default `deadline` and document/enforce it, reducing the exposure window.
- Ensure the cancellation path is only callable by the account owner (`recovered == origin-derived address`), preventing griefing by third parties, and emit an event so downstream integrators/relayers can detect invalidation.

### Proof of Concept
1. Owner `O` (Ethereum-style H160 address mapped to a Substrate account) signs a valid EIP-2612 permit for `spender = S`, `value = V`, `deadline = D`, using current `Nonces::<T>::get(contract, O) = 0`.
2. `O` shares this signature with a dApp/relayer, or it leaks (e.g., via a compromised frontend log, or is intercepted before broadcast).
3. `O` realizes the parameters are wrong (e.g., `V` too large, or wrong `S`) and wants to prevent execution. `O` has no on-chain call to bump `Nonces::<T>::get(contract, O)` — calling `permit()` itself only works with a *valid* signature for the *current* nonce, which `O` doesn't have to spend (a fresh valid permit would just create a second usable signature, not invalidate the old one, since the old signature is for nonce 0, and if `O` never submits anything, nonce stays 0).
4. At any point before `D`, `T` (an unrelated third party) submits the leaked signature through `IERC20::permitCall` → `permit()` → `use_permit()` in `lib.rs`, which succeeds because `do_verify_permit` only checks deadline and signature/owner match against the still-unconsumed nonce [9](#0-8) , and the allowance is granted for `S`/`V` [10](#0-9) .
5. `S` can now call `transferFrom` up to `V` of `O`'s tokens, with `O` having had no way to stop this despite wanting to.

### Citations

**File:** substrate/frame/assets/precompiles/src/permit.rs (L94-110)
```rust
	/// Nonces for permit signatures.
	/// Mapping: (verifying_contract, owner_address) => nonce
	///
	/// Uses Blake2_128Concat for the first key to prevent storage collision attacks
	/// when the verifying_contract address could be influenced by an attacker.
	///
	/// Note: EIP-2612 specifies uint256 nonce. We store as U256 for compatibility.
	#[pallet::storage]
	pub type Nonces<T: Config> = StorageDoubleMap<
		_,
		Blake2_128Concat,
		H160, // verifying contract address (precompile address)
		Blake2_128Concat,
		H160, // owner ethereum address
		U256, // nonce (EIP-2612 uses uint256)
		ValueQuery,
	>;
```

**File:** substrate/frame/assets/precompiles/src/permit.rs (L139-146)
```rust
		/// Increment the nonce for an owner on a specific verifying contract.
		/// Returns the new nonce value, or an error if overflow would occur.
		pub fn increment_nonce(verifying_contract: &H160, owner: &H160) -> Result<U256, Error<T>> {
			Nonces::<T>::try_mutate(verifying_contract, owner, |nonce| {
				*nonce = nonce.checked_add(U256::one()).ok_or(Error::<T>::NonceOverflow)?;
				Ok(*nonce)
			})
		}
```

**File:** substrate/frame/assets/precompiles/src/permit.rs (L301-362)
```rust
		/// Verify a permit signature without consuming it.
		///
		/// **WARNING**: This function does NOT increment the nonce. Using this
		/// function alone will leave the permit vulnerable to replay attacks.
		/// Use `use_permit` instead for production code.
		///
		/// This function is provided for cases where you need to verify a permit
		/// in a read-only context or need to separate verification from consumption.
		///
		/// The `name` parameter should be the token name per EIP-2612 specification.
		fn do_verify_permit(
			verifying_contract: &H160,
			name: &[u8],
			owner: &H160,
			spender: &H160,
			value: &[u8; 32],
			deadline: &[u8; 32],
			v: u8,
			r: &[u8; 32],
			s: &[u8; 32],
		) -> Result<(), Error<T>> {
			// EIP-2612: owner and spender cannot be the zero address
			if owner.is_zero() {
				return Err(Error::<T>::InvalidOwner);
			}
			if spender.is_zero() {
				return Err(Error::<T>::InvalidSpender);
			}

			// Validate deadline against current timestamp.
			// EIP-2612 specifies deadlines in UNIX seconds. We use the `UnixTime`
			// trait which returns a `core::time::Duration` — its `as_secs()` method
			// gives us seconds regardless of pallet_timestamp's internal resolution
			// (which stores milliseconds, converted via `Duration::from_millis` in
			// pallet_timestamp's `UnixTime` implementation).
			let now_seconds = <pallet_timestamp::Pallet<T> as UnixTime>::now().as_secs();
			let deadline_u256 = U256::from_big_endian(deadline);
			let now_u256 = U256::from(now_seconds);

			if deadline_u256 < now_u256 {
				return Err(Error::<T>::PermitExpired);
			}

			let nonce = Self::nonce(verifying_contract, owner);
			let digest = Self::permit_digest(
				verifying_contract,
				name,
				owner,
				spender,
				value,
				&nonce,
				deadline,
			);

			let recovered = Self::ecrecover(&digest, v, r, s)?;

			if &recovered != owner {
				return Err(Error::<T>::SignerMismatch);
			}

			Ok(())
		}
```

**File:** substrate/frame/assets/precompiles/src/permit.rs (L364-403)
```rust
		/// Verify and consume a permit signature atomically.
		///
		/// This is the recommended function for production use. It:
		/// 1. Validates the deadline against the current timestamp
		/// 2. Verifies the signature matches the owner
		/// 3. Increments the nonce to prevent replay attacks
		///
		/// The `name` parameter should be the token name per EIP-2612 specification.
		///
		/// After this function returns `Ok(())`, the permit cannot be used again.
		pub fn use_permit(
			verifying_contract: &H160,
			name: &[u8],
			owner: &H160,
			spender: &H160,
			value: &[u8; 32],
			deadline: &[u8; 32],
			v: u8,
			r: &[u8; 32],
			s: &[u8; 32],
		) -> Result<(), Error<T>> {
			// Verify the permit first
			Self::do_verify_permit(
				verifying_contract,
				name,
				owner,
				spender,
				value,
				deadline,
				v,
				r,
				s,
			)?;

			// Consume the permit by incrementing the nonce
			// This prevents the same permit from being used again
			Self::increment_nonce(verifying_contract, owner)?;

			Ok(())
		}
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L478-592)
```rust
	pub(crate) fn permit(
		asset_id: <Runtime as Config<Instance>>::AssetId,
		verifying_contract: H160,
		call: &IERC20::permitCall,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		// Reserve worst-case gas upfront, then refund the unused portion.
		// The total cost is: use_permit (signature verification + nonce) +
		// worst-case asset approval operations (allowance read + cancel + approve).
		let use_permit_weight = <Runtime as permit::Config>::WeightInfo::use_permit();
		let worst_case = use_permit_weight
			.saturating_add(<Runtime as Config<Instance>>::WeightInfo::allowance())
			.saturating_add(<Runtime as Config<Instance>>::WeightInfo::cancel_approval())
			.saturating_add(<Runtime as Config<Instance>>::WeightInfo::approve_transfer());
		let charged = env.charge(worst_case)?;

		let owner_h160: H160 = call.owner.into_array().into();
		let spender_h160: H160 = call.spender.into_array().into();

		// Convert U256 values to byte arrays
		let value_bytes: [u8; 32] = call.value.to_be_bytes();
		let deadline_bytes: [u8; 32] = call.deadline.to_be_bytes();
		let r_bytes: [u8; 32] = call.r.0;
		let s_bytes: [u8; 32] = call.s.0;

		let transaction_outcome = frame_support::storage::with_transaction(|| {
			let result = (|| {
				// Use the permit - this validates deadline, signature, and increments nonce
				permit::Pallet::<Runtime>::use_permit(
					&verifying_contract,
					&pallet_assets::Pallet::<Runtime, Instance>::name(asset_id.clone()),
					&owner_h160,
					&spender_h160,
					&value_bytes,
					&deadline_bytes,
					call.v,
					&r_bytes,
					&s_bytes,
				)
				.map_err(|e| {
					let msg = match e {
						permit::pallet::Error::PermitExpired => "Permit expired",
						permit::pallet::Error::InvalidSignature => "Invalid signature",
						permit::pallet::Error::SignerMismatch => "Signer does not match owner",
						permit::pallet::Error::SignatureSValueTooHigh => {
							"Signature s value too high (malleability)"
						},
						permit::pallet::Error::InvalidVValue => "Invalid signature v value",
						permit::pallet::Error::NonceOverflow => "Nonce overflow",
						permit::pallet::Error::InvalidOwner => "Invalid owner address",
						permit::pallet::Error::InvalidSpender => "Invalid spender address",
					};
					Error::Revert(Revert { reason: msg.into() })
				})?;

				// Delete-set semantic: cancel any existing approval first so
				// do_approve_transfer sets (not accumulates) the new value.
				use frame_support::traits::fungibles::approvals::Inspect as ApprovalsInspect;
				let owner_account =
					<Runtime as pallet_revive::Config>::AddressMapper::to_account_id(&owner_h160);
				let spender_account =
					<Runtime as pallet_revive::Config>::AddressMapper::to_account_id(&spender_h160);

				// Saturate: see `approve` for the rationale (infinite-allowance idiom).
				let new_amount: <Runtime as Config<Instance>>::Balance =
					call.value.unique_saturated_into();
				let current = pallet_assets::Pallet::<Runtime, Instance>::allowance(
					asset_id.clone(),
					&owner_account,
					&spender_account,
				);

				let actual_weight;
				if new_amount.is_zero() {
					if !current.is_zero() {
						// clear approval if it exists, to match ERC-20 semantics of setting
						// allowance to 0
						pallet_assets::Pallet::<Runtime, Instance>::do_cancel_approval(
							&asset_id,
							&owner_account,
							&spender_account,
						)?;
						actual_weight = use_permit_weight
							.saturating_add(<Runtime as Config<Instance>>::WeightInfo::allowance())
							.saturating_add(
								<Runtime as Config<Instance>>::WeightInfo::cancel_approval(),
							);
					} else {
						// noop: set allowance to zerowhen it is already zero
						actual_weight = use_permit_weight
							.saturating_add(<Runtime as Config<Instance>>::WeightInfo::allowance());
					}
				} else {
					if !current.is_zero() {
						// If there's an existing non-zero allowance, cancel it first
						pallet_assets::Pallet::<Runtime, Instance>::do_cancel_approval(
							&asset_id,
							&owner_account,
							&spender_account,
						)?;
						actual_weight = worst_case;
					} else {
						// set new approval
						actual_weight = use_permit_weight
							.saturating_add(<Runtime as Config<Instance>>::WeightInfo::allowance())
							.saturating_add(
								<Runtime as Config<Instance>>::WeightInfo::approve_transfer(),
							);
					}
					pallet_assets::Pallet::<Runtime, Instance>::do_approve_transfer(
						asset_id,
						&owner_account,
						&spender_account,
						new_amount,
					)?;
```

**File:** substrate/frame/system/src/extensions/check_nonce.rs (L93-105)
```rust
	/// In transaction extension, prepare nonce for account.
	pub fn prepare_nonce_for_account(
		who: &T::AccountId,
		mut nonce: T::Nonce,
	) -> Result<(), TransactionValidityError> {
		let account = crate::Account::<T>::get(who);
		if nonce > account.nonce {
			return Err(InvalidTransaction::Future.into());
		}
		nonce = nonce.checked_add(&T::Nonce::one()).unwrap_or(T::Nonce::zero());
		crate::Account::<T>::mutate(who, |account| account.nonce = nonce);
		Ok(())
	}
```

**File:** substrate/frame/meta-tx/src/tests.rs (L304-312)
```rust
		// increment alice's nonce to invalidate the meta tx and verify that the
		// meta tx extension works.
		frame_system::Pallet::<Runtime>::inc_account_nonce(alice_account.clone());

		// Check Extrinsic validity and apply it.
		let result = apply_extrinsic(uxt);

		// Asserting the results.
		assert_eq!(result.unwrap_err().error, Error::<Runtime>::Stale.into());
```
