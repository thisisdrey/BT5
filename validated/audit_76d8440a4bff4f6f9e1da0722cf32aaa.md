### Title
Permissionless `SafeMode::enter` lets any funded account halt slashing enforcement and withdrawals chain-wide - (File: `substrate/frame/safe-mode/src/lib.rs`, `substrate/bin/node/runtime/src/lib.rs`)

### Summary
The external report's core issue is that a semi-trusted "manager" role can unilaterally pause enforcement actions (slashing) and user withdrawals with no counter-balancing check. The Substrate `pallet-safe-mode` reproduces this exact broken invariant, but with an even weaker trust assumption: **any signed account** holding a modest, fully-refundable deposit can pause virtually all runtime calls — including staking slash-enforcement (`apply_slash`) and unbonding/withdrawal extrinsics — for a configurable number of blocks, with no governance or admin step involved at all.

### Finding Description
`pallet-safe-mode` exposes a permissionless dispatchable `enter` that only requires a signed origin and a deposit: [1](#0-0) 

Once entered, `Pallet::is_allowed` filters every call in the runtime except those explicitly whitelisted by `Config::WhitelistedCalls`: [2](#0-1) 

In the reference runtime configuration, the whitelist (`SafeModeWhitelistedCalls`) permits only `System`, `SafeMode`, and `TxPause` calls to bypass the filter — every other pallet, including `Staking` and `Balances`, is blocked while safe-mode is active: [3](#0-2) 

This means that, with this filter wired into `frame_system::Config::BaseCallFilter`, a single unprivileged account that can afford `EnterDepositAmount` (a *hold*, not a burn — it is later released) can call `SafeMode::enter` and immediately block:
- `pallet-staking`'s permissionless `apply_slash` (the exact fallback mechanism meant to guarantee slashes get applied — see `substrate/frame/staking-async/src/pallet/mod.rs:3065-3087`),
- `withdraw_unbonded` / `unbond` on both `pallet-staking` and `pallet-nomination-pools`,
- ordinary `Balances::transfer`.

The deposit can further be renewed via the also-permissionless `extend` call, and the mode automatically re-armable once released, so the attacker's cost is only the opportunity cost of a temporarily locked (and later fully returned) balance, not a burn.

### Impact Explanation
This directly reproduces the reported bug class — an under-trusted actor pausing slashing enforcement and withdrawals — but is strictly worse: the vault report at least required a designated "manager" role; here **no privileged role is required at all**. Any account with sufficient free balance can:
1. Prevent the permissionless `apply_slash` fallback from running, letting `UnappliedSlashes` accumulate and blocking further withdrawals network-wide (per the `UnappliedSlashesInPreviousEra` guard added in `pallet-staking-async`, see `prdoc/stable2509/pr_9079.prdoc`), and
2. Block ordinary user balance transfers and unbonding/withdrawal calls for the duration of `EnterDuration`, i.e. degrade/stall normal chain operation for all users, matching the "public underpriced work that degrades block production or stalls processing" impact class.

### Likelihood Explanation
Likelihood is high wherever a runtime wires `SafeMode` into `BaseCallFilter` with a broad (default-deny) whitelist as done in `substrate/bin/node/runtime/src/lib.rs`, and configures `EnterDepositAmount` to `Some(_)` (permissionless entry enabled). No colluding validator, relayer, or governance actor is needed — a single signed transaction from any account with the deposit amount suffices, and it can be repeated indefinitely by extending or re-entering after each `EnterDuration` window and reclaiming the deposit.

### Recommendation
- Ensure any runtime enabling `pallet-safe-mode`'s permissionless `enter`/`extend` (non-`None` `EnterDepositAmount`/`ExtendDepositAmount`) whitelists all calls whose availability is safety-critical for existing debt/slash/withdrawal guarantees (e.g. `Staking::apply_slash`, `Staking::withdraw_unbonded`, nomination-pools withdrawal/slash-application calls) so they remain callable even while safe-mode is active.
- Alternatively, disable permissionless `enter`/`extend` (`EnterDepositAmount = None`) and require `ForceEnterOrigin`/`ForceExtendOrigin` (a governance-style origin) for triggering safe-mode, removing the untrusted-single-account attack surface entirely.
- Consider making the enter/extend deposit scale with `EnterDuration` and be partially burned rather than fully refundable, to remove the "underpriced" griefing incentive.

### Proof of Concept
1. Runtime configures `pallet-safe-mode` with `EnterDepositAmount = Some(D)` and `WhitelistedCalls` limited to `System`/`SafeMode`/`TxPause` (as in `substrate/bin/node/runtime/src/lib.rs:247-256`).
2. Attacker funds an account with balance ≥ `D`.
3. Attacker submits `SafeMode::enter(origin)` (`substrate/frame/safe-mode/src/lib.rs:306-310`); `EnteredUntil` is set to `now + EnterDuration`.
4. For the next `EnterDuration` blocks, any call to `Staking::apply_slash`, `Staking::withdraw_unbonded`, nomination-pools withdrawal/slash calls, or `Balances::transfer` is rejected with `frame_system::Error::CallFiltered` because `Pallet::is_allowed` returns `false` for non-whitelisted calls (`substrate/frame/safe-mode/src/lib.rs:582-598`).
5. Before expiry, the attacker calls `SafeMode::extend(origin)` (if `ExtendDepositAmount` is configured) to prolong the block, or simply re-enters after `EnteredUntil` lapses and their deposit is released, repeating the DoS at will.

### Citations

**File:** substrate/frame/safe-mode/src/lib.rs (L296-310)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T> {
		/// Enter safe-mode permissionlessly for [`Config::EnterDuration`] blocks.
		///
		/// Reserves [`Config::EnterDepositAmount`] from the caller's account.
		/// Emits an [`Event::Entered`] event on success.
		/// Errors with [`Error::Entered`] if the safe-mode is already entered.
		/// Errors with [`Error::NotConfigured`] if the deposit amount is `None`.
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::enter())]
		pub fn enter(origin: OriginFor<T>) -> DispatchResult {
			let who = ensure_signed(origin)?;

			Self::do_enter(Some(who), T::EnterDuration::get()).map_err(Into::into)
		}
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

**File:** substrate/bin/node/runtime/src/lib.rs (L247-256)
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
```
