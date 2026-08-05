## Analysis

The external report's core broken invariant: a **pausing mechanism with no time-bound recourse** blocks a **permissionless value-claim path**, and the entity that triggers the pause is not required to be trusted/benevolent forever. The closest unprivileged analog in `polkadot-sdk` is `pallet-safe-mode`, whose `enter`/`extend` calls are explicitly **permissionless** (unlike `pallet-tx-pause`, which is `Root`-gated and therefore falls under the discarded "admin abuse" category).

### Title
Permissionless, indefinitely-renewable `pallet-safe-mode::extend` lets any funded account block all non-whitelisted claim/payout extrinsics chain-wide - (File: `substrate/frame/safe-mode/src/lib.rs`)

### Summary
`pallet-safe-mode::enter` and `pallet-safe-mode::extend` can be called by **any signed account**, not just governance, as long as `EnterDepositAmount`/`ExtendDepositAmount` are configured (which the node runtime does: 2,000,000 / 1,000,000 DOLLARS respectively) [1](#0-0) . Once safe-mode is active, `Contains::contains` filters out every call except those in `WhitelistedCalls`, which in the reference runtime is limited to `System`, `SafeMode`, and `TxPause` [2](#0-1) . This means all fund-claim extrinsics — `Vesting::vest`, `NominationPools::claim_payout`, `Staking::payout_stakers`, treasury payouts, etc. — are blocked while safe-mode is entered, exactly mirroring the reported bug where a "pause" blocks users from claiming already-earned/vested tokens, except here the trigger requires no admin privilege at all.

### Finding Description
`do_enter` sets `EnteredUntil` to `now + duration`, and only exits automatically via `on_initialize` once `current > limit`, or early via `T::ForceExitOrigin` (root-only) [3](#0-2) [4](#0-3) . Critically, `do_extend` is also callable permissionlessly (`Pallet::extend`) and simply **accrues** more blocks onto `EnteredUntil` for the price of `ExtendDepositAmount`, with no cap on how many times it can be extended [5](#0-4) [6](#0-5) . There is **no permissionless exit** — `Pallet::force_exit` requires `ForceExitOrigin` (root) [7](#0-6) .

The deposits are held (`Currency::hold`), not burned, and are returned via `release_deposit`/`force_release_deposit` after `ReleaseDelay` — so the attacker's capital is not destroyed, only temporarily locked, making sustained griefing economically viable for any actor with idle capital or an incentive to disrupt claims (e.g. a competing validator/exchange wanting to freeze withdrawals, or a short-seller wanting to prevent unlock-driven sell pressure).

Because `is_allowed`/`Contains::contains` is wired into `BaseCallFilter` at the runtime level [8](#0-7) , this filter applies uniformly to **every dispatchable in the runtime**, with no per-pallet carve-out for "user's own already-earned funds" (the exact class of call the external report says must remain callable). Existing guards (`ForceExitOrigin`) do not help, since exiting requires root — an unprivileged attacker who keeps paying `ExtendDepositAmount` before each expiry can keep the chain in safe-mode indefinitely without needing any admin cooperation, and without needing to be malicious in the sense of compromising a validator/relayer/node (which are explicitly out of scope) — they only need funds and a signed account, which is the "unprivileged attacker" class explicitly in-scope.

### Impact Explanation
While active, **all** balance/reward/vesting claim extrinsics across every pallet in the runtime are blocked, matching "public underpriced work that degrades block production or stalls ... processing" and effectively a chain-wide, attacker-renewable fund-claim lock — a stronger version of the original report's "owner pauses claims" since here *no privileged role is required at all*.

### Likelihood Explanation
Requires only: (1) a signed account, (2) enough balance to cover `EnterDepositAmount`/`ExtendDepositAmount` (held, not spent), which is returned later. No validator, collator, relayer, or governance compromise needed — fitting the in-scope "unprivileged attacker" criteria.

### Recommendation
- Add an explicit, permissionless "safe" whitelist category (similar to `TxPauseWhitelistedCalls`'s `Balances::transfer_keep_alive` carve-out) that always allows claim-only extrinsics (`Vesting::vest`, `NominationPools::claim_payout`, `Staking::payout_stakers`, etc.) to bypass `SafeMode`, regardless of state.
- Consider capping the total permissionless-extendable duration (e.g., a hard ceiling on cumulative `extend` calls) so safe-mode cannot be perpetually renewed by a single non-privileged actor.
- Increase `ExtendDepositAmount` cost scaling (e.g., increasing per extension) to make indefinite griefing economically prohibitive, or require slashing rather than mere holding of the deposit if extended beyond a threshold without governance ratification.

### Proof of Concept
1. Attacker account with balance ≥ `EnterDepositAmount` calls `SafeMode::enter` → `EnteredUntil = now + EnterDuration` (4 hours in the reference runtime) [9](#0-8) .
2. Any user attempting `Vesting::vest`, `NominationPools::claim_payout`, etc. is rejected via `CallFiltered` because `SafeModeWhitelistedCalls` only allows `System`/`SafeMode`/`TxPause` [2](#0-1) .
3. Before `EnteredUntil` expires, attacker (or a colluding set of funded accounts) calls `SafeMode::extend` repeatedly, each call pushing `EnteredUntil` forward by `ExtendDuration` (2 hours) [6](#0-5) .
4. No root/`ForceExitOrigin` account exists that is forced to intervene; if governance is slow or unresponsive, claim-blocking persists indefinitely at the cost of held (non-burned) deposits, which are refunded once the attacker stops extending. [10](#0-9)

### Citations

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

**File:** substrate/frame/safe-mode/src/lib.rs (L326-343)
```rust
		/// Extend the safe-mode permissionlessly for [`Config::ExtendDuration`] blocks.
		///
		/// This accumulates on top of the current remaining duration.
		/// Reserves [`Config::ExtendDepositAmount`] from the caller's account.
		/// Emits an [`Event::Extended`] event on success.
		/// Errors with [`Error::Exited`] if the safe-mode is entered.
		/// Errors with [`Error::NotConfigured`] if the deposit amount is `None`.
		///
		/// This may be called by any signed origin with [`Config::ExtendDepositAmount`] free
		/// currency to reserve. This call can be disabled for all origins by configuring
		/// [`Config::ExtendDepositAmount`] to `None`.
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::extend())]
		pub fn extend(origin: OriginFor<T>) -> DispatchResult {
			let who = ensure_signed(origin)?;

			Self::do_extend(Some(who), T::ExtendDuration::get()).map_err(Into::into)
		}
```

**File:** substrate/frame/safe-mode/src/lib.rs (L345-358)
```rust
		/// Extend the safe-mode by force for a per-origin configured number of blocks.
		///
		/// Emits an [`Event::Extended`] event on success.
		/// Errors with [`Error::Exited`] if the safe-mode is inactive.
		///
		/// Can only be called by the [`Config::ForceExtendOrigin`] origin.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::force_extend())]
		pub fn force_extend(origin: OriginFor<T>) -> DispatchResult {
			let duration = T::ForceExtendOrigin::ensure_origin(origin)?;

			Self::do_extend(None, duration).map_err(Into::into)
		}

```

**File:** substrate/frame/safe-mode/src/lib.rs (L445-461)
```rust
	#[pallet::hooks]
	impl<T: Config> Hooks<BlockNumberFor<T>> for Pallet<T> {
		/// Automatically exits safe-mode when the current block number is greater than
		/// [`EnteredUntil`].
		fn on_initialize(current: BlockNumberFor<T>) -> Weight {
			let Some(limit) = EnteredUntil::<T>::get() else {
				return T::WeightInfo::on_initialize_noop();
			};

			if current > limit {
				let _ = Self::do_exit(ExitReason::Timeout).defensive_proof("Only Errors if safe-mode is not entered. Safe-mode has already been checked to be entered; qed");
				T::WeightInfo::on_initialize_exit()
			} else {
				T::WeightInfo::on_initialize_noop()
			}
		}
	}
```

**File:** substrate/frame/safe-mode/src/lib.rs (L464-482)
```rust
impl<T: Config> Pallet<T> {
	/// Logic for the [`crate::Pallet::enter`] and [`crate::Pallet::force_enter`] calls.
	pub(crate) fn do_enter(
		who: Option<T::AccountId>,
		duration: BlockNumberFor<T>,
	) -> Result<(), Error<T>> {
		ensure!(!Self::is_entered(), Error::<T>::Entered);

		if let Some(who) = who {
			let amount = T::EnterDepositAmount::get().ok_or(Error::<T>::NotConfigured)?;
			Self::hold(who, amount)?;
		}

		let until = <frame_system::Pallet<T>>::block_number().saturating_add(duration);
		EnteredUntil::<T>::put(until);
		Self::deposit_event(Event::Entered { until });
		T::Notify::entered();
		Ok(())
	}
```

**File:** substrate/frame/safe-mode/src/lib.rs (L484-500)
```rust
	/// Logic for the [`crate::Pallet::extend`] and [`crate::Pallet::force_extend`] calls.
	pub(crate) fn do_extend(
		who: Option<T::AccountId>,
		duration: BlockNumberFor<T>,
	) -> Result<(), Error<T>> {
		let mut until = EnteredUntil::<T>::get().ok_or(Error::<T>::Exited)?;

		if let Some(who) = who {
			let amount = T::ExtendDepositAmount::get().ok_or(Error::<T>::NotConfigured)?;
			Self::hold(who, amount)?;
		}

		until.saturating_accrue(duration);
		EnteredUntil::<T>::put(until);
		Self::deposit_event(Event::<T>::Extended { until });
		Ok(())
	}
```

**File:** substrate/frame/safe-mode/src/lib.rs (L577-609)
```rust
	/// Return whether `safe-mode` is entered.
	pub fn is_entered() -> bool {
		EnteredUntil::<T>::exists()
	}

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
}

impl<T: Config> Contains<T::RuntimeCall> for Pallet<T>
where
	T::RuntimeCall: GetCallMetadata,
{
	/// Return whether the given call is allowed to be dispatched.
	fn contains(call: &T::RuntimeCall) -> bool {
		Pallet::<T>::is_allowed(call)
	}
}
```
