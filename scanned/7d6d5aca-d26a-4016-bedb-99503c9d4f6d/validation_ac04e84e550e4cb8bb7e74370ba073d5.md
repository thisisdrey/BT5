### Title
SafeMode / TxPause call-filtering blocks user self-protection (`unbond`/`chill`) while automatic deferred-slash application still executes via `on_initialize`, unfairly "liquidating" stakers - (File: `substrate/frame/safe-mode/src/lib.rs`, `substrate/frame/staking-async/src/slashing.rs`)

### Summary
The external report's core broken invariant is: a downtime/grace-period gate blocks the user's *protective* action (create/fill order to avoid liquidation) while the *adverse* state transition (liquidation) is still allowed to occur once the gate lifts, giving the user no chance to react. The same asymmetry exists in Substrate's `pallet-safe-mode` / `pallet-tx-pause` combo as used by the reference node runtime: when safe-mode is entered, `BaseCallFilter` blocks essentially all extrinsics except a small whitelist, which does **not** include staking self-protection calls (`chill`, `unbond`, `withdraw_unbonded`). Meanwhile, deferred slash application in `pallet-staking-async` happens automatically inside `on_initialize`, which is a block-execution hook, not a dispatched extrinsic — so it is completely unaffected by `BaseCallFilter`/safe-mode/tx-pause.

### Finding Description
`pallet-safe-mode`'s `Pallet::is_allowed` gates *all* dispatched calls not in `T::WhitelistedCalls`: [1](#0-0) 

In the reference node runtime, the whitelist used for safe-mode only exempts `System`, `SafeMode`, and `TxPause`, and the tx-pause whitelist only exempts `Balances::transfer_keep_alive`: [2](#0-1) 

This means once safe-mode is entered, any signed call to `Staking::chill`, `Staking::unbond`, or `Staking::withdraw_unbonded` is rejected with `CallFiltered` — a staker who wants to exit exposure has no way to do so during the window.

Safe-mode can be entered *permissionlessly* by anyone willing to place a deposit (`EnterDepositAmount`), not just by governance: [3](#0-2) [4](#0-3) 

At the same time, `pallet-staking-async` applies deferred slashes automatically once the deferral window elapses, driven entirely by era rotation / `on_initialize` processing rather than any extrinsic: [5](#0-4) [6](#0-5) 

`apply_slash`/`do_slash` execute unconditionally when the deferred era is reached — they never check `SafeMode::is_entered()` or any pause state, because they run as internal pallet logic invoked from a hook, not as a filtered `RuntimeCall`: [7](#0-6) 

The staking documentation itself confirms the slash timeline is driven purely by era counters, independent of any call-level gating: [8](#0-7) 

### Impact Explanation
If safe-mode (or tx-pause) is active — which any account can trigger by placing the configured deposit — while a validator has an already-computed, pending deferred slash approaching its application era, affected nominators/validators are prevented from calling `chill`/`unbond`/`withdraw_unbonded` to reduce or exit their exposure, yet the slash still applies automatically once the era arrives. This is functionally identical to the reported bug class: a downtime/grace gate blocks the user's defensive action while the adverse state change (loss of funds via slashing) proceeds unimpeded, resulting in unfair, unavoidable fund loss for stakers who would otherwise have exited in time.

### Likelihood Explanation
Entering safe-mode is permissionless (deposit-gated, not requiring governance), so any actor can trigger the window; combined with the fact that `SlashDeferDuration` is public and offences/slash computation events are observable on-chain, an attacker (or even an incidental safe-mode activation for an unrelated emergency) can create or coincide with a window where targeted stakers are unable to react to an already-queued slash. The whitelist configuration shown is from the reference/test node runtime; any production runtime reusing the same whitelist pattern (exempting only `System`/`SafeMode`/`TxPause`) is exposed identically.

### Recommendation
- Add staking exit/self-protection calls (`chill`, `unbond`, `withdraw_unbonded`, and pool-equivalent calls) to the `WhitelistedCalls` for both `pallet-safe-mode` and `pallet-tx-pause`, so users retain the ability to reduce exposure during an emergency halt.
- Alternatively/additionally, gate automatic slash application (`apply_slash`) on the safe-mode state, deferring slash application while the runtime is in safe-mode, symmetric to how user calls are deferred.
- Document and test explicitly that "protective" calls remain available whenever automatic adverse state transitions (slashing) can still occur.

### Proof of Concept
1. Configure a runtime as in `substrate/bin/node/runtime` with `pallet-staking-async` (`SlashDeferDuration = D > 0`) and `pallet-safe-mode`/`pallet-tx-pause` using the whitelist shown above.
2. An offence for validator `V` is reported/processed in era `E`; `SlashComputed` event shows `slash_era = E + D` (see test pattern at `substrate/frame/staking-async/src/tests/slashing.rs:400-450`).
3. Shortly before era `E + D`, any account calls `SafeMode::enter` with the required deposit, entering safe-mode for `EnterDuration` blocks.
4. A nominator of `V` attempts `Staking::chill`/`unbond`/`withdraw_unbonded` to reduce exposure before the slash lands — this fails with `frame_system::Error::CallFiltered`, as demonstrated by the analogous test `can_filter_balance_calls_when_activated` for `Balances::transfer` (`substrate/frame/safe-mode/src/tests.rs:138-148`); the same filtering applies to any non-whitelisted `Staking` call.
5. Era `E + D` begins; `on_initialize` in `pallet-staking-async` processes `UnappliedSlashes`/offence queue and calls `apply_slash`/`do_slash` unconditionally (`substrate/frame/staking-async/src/slashing.rs:269-290`, `:584-619`), slashing the nominator's stake even though safe-mode is still active and the nominator was blocked from exiting.

### Citations

**File:** substrate/frame/safe-mode/src/lib.rs (L27-40)
```rust
//! ## Overview
//!
//! Safe mode is entered via two paths (deposit or forced) until a set block number.
//! The mode is exited when the block number is reached or a call to one of the exit extrinsics is
//! made. A `WhitelistedCalls` configuration item contains all calls that can be executed while in
//! safe mode.
//!
//! ### Primary Features
//!
//! - Entering safe mode can be via privileged origin or anyone who places a deposit.
//! - Origin configuration items are separated for privileged entering and exiting safe mode.
//! - A configurable duration sets the number of blocks after which the system will exit safe mode.
//! - Safe mode may be extended beyond the configured exit by additional calls.
//!
```

**File:** substrate/frame/safe-mode/src/lib.rs (L582-598)
```rust
	/// Return whether the given call is allowed to be dispatched.
	pub fn is_allowed(call: &T::RuntimeCall) -> bool
	where
		T::RuntimeCall: GetCallMetadata,
	{
		let CallMetadata { pallet_name, .. } = call.get_call_metadata();
		// SAFETY: The `SafeMode` pallet is always allowed.
		if pallet_name == <Pallet<T> as PalletInfoAccess>::name() {
			return true;
		}

		if Self::is_entered() {
			T::WhitelistedCalls::contains(call)
		} else {
			true
		}
	}
```

**File:** substrate/bin/node/runtime/src/lib.rs (L247-268)
```rust
/// Calls that can bypass the safe-mode pallet.
pub struct SafeModeWhitelistedCalls;
impl Contains<RuntimeCall> for SafeModeWhitelistedCalls {
	fn contains(call: &RuntimeCall) -> bool {
		match call {
			RuntimeCall::System(_) | RuntimeCall::SafeMode(_) | RuntimeCall::TxPause(_) => true,
			_ => false,
		}
	}
}

/// Calls that cannot be paused by the tx-pause pallet.
pub struct TxPauseWhitelistedCalls;
/// Whitelist `Balances::transfer_keep_alive`, all others are pauseable.
impl Contains<RuntimeCallNameOf<Runtime>> for TxPauseWhitelistedCalls {
	fn contains(full_name: &RuntimeCallNameOf<Runtime>) -> bool {
		match (full_name.0.as_slice(), full_name.1.as_slice()) {
			(b"Balances", b"transfer_keep_alive") => true,
			_ => false,
		}
	}
}
```

**File:** substrate/bin/node/runtime/src/lib.rs (L329-335)
```rust
parameter_types! {
	pub const EnterDuration: BlockNumber = 4 * HOURS;
	pub const EnterDepositAmount: Balance = 2_000_000 * DOLLARS;
	pub const ExtendDuration: BlockNumber = 2 * HOURS;
	pub const ExtendDepositAmount: Balance = 1_000_000 * DOLLARS;
	pub const ReleaseDelay: u32 = 2 * DAYS;
}
```

**File:** substrate/frame/staking-async/src/lib.rs (L130-142)
```rust
//! ### Phase 4: Application
//!
//! Based on `SlashDeferDuration`, slashes are either:
//!
//! **Immediate (SlashDeferDuration = 0)**:
//! - Applied right away in the same block
//! - Funds deducted from staking ledger immediately
//!
//! **Deferred (SlashDeferDuration > 0)**:
//! - Stored in `UnappliedSlashes` for future application
//! - Applied at era: `offence_era + SlashDeferDuration`
//! - Can be cancelled by governance before application
//!
```

**File:** substrate/frame/staking-async/src/lib.rs (L169-196)
```rust
//! **Withdrawal Timeline Example with an Offence**:
//! ```text
//! Era:        90    91    92    93    94    95    96    97    98    99    100   ...  117   118
//!             |     |     |     |     |     |     |     |     |     |     |          |     |
//! Unbond:     U
//! Offence:    X
//! Reported:               R
//! Processed:              P (within next few blocks)
//! Slash Applied:                                                                       S
//! Withdraw:                                                                            ❌    ✓
//!
//! With BondingDuration = 28 and SlashDeferDuration = 27:
//! - User unbonds in era 90
//! - Offence occurs in era 90
//! - Reported in era 92 (typically within 2 days, but reportable until Era 116)
//! - Processed in era 92 (within next few blocks after reporting)
//! - Slash deferred for 27 eras, applied at era 117 (90 + 27)
//! - Cannot withdraw unbonded chunks until era 118 (90 + 28)
//!
//! The 28-era bonding duration ensures that any offences committed before or during
//! unbonding have time to be reported, processed, and applied before funds can be
//! withdrawn. This provides a window for governance to cancel slashes that may have
//! resulted from software bugs.
//! ```
//!
//! **Key Restrictions**:
//! 1. Cannot withdraw if previous era has unapplied slashes
//! 2. Cannot withdraw funds from eras with unprocessed offences
```

**File:** substrate/frame/staking-async/src/slashing.rs (L269-290)
```rust
	} else {
		// Historical Note: Previously, with BondingDuration = 28 and SlashDeferDuration = 27,
		// slashes were applied at the start of the 28th era from `offence_era`.
		// However, with paged slashing, applying slashes now takes multiple blocks.
		// To account for this delay, slashes are now applied at the start of the 27th era from
		// `offence_era`.
		log!(
			debug,
			"🦹 deferring slash of {:?}% happened in {:?} (reported in {:?}) to {:?}",
			offence_record.slash_fraction,
			offence_era,
			offence_record.reported_era,
			slash_era,
		);
		UnappliedSlashes::<T>::insert(
			slash_era,
			(offender, offence_record.slash_fraction, slash_page),
			unapplied,
		);
		T::WeightInfo::process_offence_queue()
	}
}
```

**File:** substrate/frame/staking-async/src/slashing.rs (L584-619)
```rust
pub fn do_slash<T: Config>(
	stash: &T::AccountId,
	value: BalanceOf<T>,
	reward_payout: &mut BalanceOf<T>,
	slashed_imbalance: &mut NegativeImbalanceOf<T>,
	offence_era: EraIndex,
) {
	let mut ledger =
		match Pallet::<T>::ledger(sp_staking::StakingAccount::Stash(stash.clone())).defensive() {
			Ok(ledger) => ledger,
			Err(_) => return, // nothing to do.
		};

	let value = ledger.slash(value, asset::existential_deposit::<T>(), offence_era);
	if value.is_zero() {
		// nothing to do
		return;
	}

	// Skip slashing for virtual stakers. The pallets managing them should handle the slashing.
	if !Pallet::<T>::is_virtual_staker(stash) {
		let (imbalance, missing) = asset::slash::<T>(stash, value);
		slashed_imbalance.subsume(imbalance);

		if !missing.is_zero() {
			// deduct overslash from the reward payout
			*reward_payout = reward_payout.saturating_sub(missing);
		}
	}

	let _ = ledger
		.update()
		.defensive_proof("ledger fetched from storage so it exists in storage; qed.");

	// trigger the event
	<Pallet<T>>::deposit_event(super::Event::<T>::Slashed { staker: stash.clone(), amount: value });
```
