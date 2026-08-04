Based on my investigation, I was unable to establish a locally-provable analog that meets the strict bar required by the Impact Gate.

The external report's core broken invariant is: a refactor replaced a permissive "sentinel value (0) bypasses the time check" branch with a single unconditional strict inequality, silently making a documented, intentionally-supported state (`expirationTime == 0` for oracle cancellation) permanently unusable.

I searched the repository for structurally similar patterns — expiry/deadline checks, mortality checks, EIP-2612 permit deadlines, bounty `update_due`/`BountyUpdatePeriod` expiry logic, multisig timepoints, relayer `valid_till`, nomination-pool claim permissions, and parachain scheduler lookahead — looking for a case where:
- a "0 / sentinel means unbounded or special-cased" semantic is documented or implied, and
- a strict comparison against current time/block silently and permanently rejects that sentinel, causing legitimate operations to always fail (a liveness/fund-lock bug), not just a cosmetic one.

None of the candidates found actually reproduce this pattern with exploitable impact:

- `substrate/frame/assets/precompiles/src/permit.rs` `do_verify_permit` (lines 330-342) implements the EIP-2612 deadline check as `deadline_u256 < now_u256 → PermitExpired`, which is the correct, standard OZ semantic (`deadline == now` is accepted; `deadline` in the past is rejected). There is no "0 means unlimited" sentinel here, and no regression versus a prior looser check was found. [1](#0-0) 
- `substrate/frame/bounties/src/lib.rs` explicitly documents and implements `BlockNumberFor::MAX` (not 0) as the "never expire" sentinel for `BountyUpdatePeriod`, and the refactor noted in `prdoc/stable2503/pr_7723.prdoc` moved to `saturating_add` specifically to fix an overflow that would have broken this sentinel — this is the opposite of the reported bug class (a fix, not an introduced break), and it requires no attacker-controlled input beyond privileged root configuration. [2](#0-1) [3](#0-2) 
- `bridges/modules/relayers/src/lib.rs` `register`/`deregister` use `valid_till` with strict/inclusive comparisons that were consistent and did not show a sentinel-related regression. [4](#0-3) 
- `substrate/frame/system/src/extensions/check_mortality.rs` correctly distinguishes "immortal" transactions via a dedicated `is_immortal()` predicate rather than overloading a numeric sentinel inside a single inequality, so the Blur-style collapse-into-one-branch bug class does not apply. [5](#0-4) 
- `substrate/client/hop/src/types.rs` has a genuinely similar-looking sentinel (`next_promotion_attempt_at`, "`0` means any tick"), but this is client-side offchain maintenance-task scheduling metadata (`substrate/client/hop`), not a public runtime dispatch, proof-verification, or asset-accounting path, and I was not able to fully verify its comparison logic in `pool.rs` before running out of iterations — so I cannot respons

### Citations

**File:** substrate/frame/assets/precompiles/src/permit.rs (L330-342)
```rust
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
```

**File:** prdoc/stable2503/pr_7723.prdoc (L1-8)
```text
title: '[pallet-bounties] Allow bounties to never expire'
doc:
- audience: Runtime Dev
  description: |
    Refactored the `update_due` calculation to use `saturating_add`, allowing bounties to remain active   
    indefinitely without requiring `extend_bounty_expiry` and preventing automatic curator slashing for  
    inactivity. Previously, setting `BountyUpdatePeriod` to a large value, such as `BlockNumber::max_value()`,  
    could cause an overflow. 
```

**File:** substrate/frame/bounties/src/lib.rs (L339-346)
```rust
		/// The time limit for a curator to act before a bounty expires.
		///
		/// The period that starts when a curator is approved, during which they must execute or
		/// update the bounty via `extend_bounty_expiry`. If missed, the bounty expires, and the
		/// curator may be slashed. If `BlockNumberFor::MAX`, bounties stay active indefinitely,
		/// removing the need for `extend_bounty_expiry`.
		#[pallet::constant]
		type BountyUpdatePeriod: Get<BlockNumberFor<Self, I>>;
```

**File:** bridges/modules/relayers/src/lib.rs (L126-148)
```rust
		pub fn register(origin: OriginFor<T>, valid_till: BlockNumberFor<T>) -> DispatchResult {
			let relayer = ensure_signed(origin)?;

			// valid till must be larger than the current block number and the lease must be larger
			// than the `RequiredRegistrationLease`
			let lease = valid_till.saturating_sub(frame_system::Pallet::<T>::block_number());
			ensure!(
				lease > Self::required_registration_lease(),
				Error::<T, I>::InvalidRegistrationLease
			);

			RegisteredRelayers::<T, I>::try_mutate(
				&relayer,
				|maybe_registration| -> DispatchResult {
					let mut registration = maybe_registration
						.unwrap_or_else(|| Registration { valid_till, stake: Zero::zero() });

					// new `valid_till` must be larger (or equal) than the old one
					ensure!(
						valid_till >= registration.valid_till,
						Error::<T, I>::CannotReduceRegistrationLease,
					);
					registration.valid_till = valid_till;
```

**File:** substrate/frame/system/src/extensions/check_mortality.rs (L76-85)
```rust
	fn weight(&self, _: &T::RuntimeCall) -> sp_weights::Weight {
		if self.0.is_immortal() {
			// All immortal transactions will always read the hash of the genesis block, so to avoid
			// charging this multiple times in a block we manually set the proof size to 0.
			<T::ExtensionsWeightInfo as super::WeightInfo>::check_mortality_immortal_transaction()
				.set_proof_size(0)
		} else {
			<T::ExtensionsWeightInfo as super::WeightInfo>::check_mortality_mortal_transaction()
		}
	}
```
