## Analysis

The Aera report's core broken invariant is: **a permissionless, per-user timeout accumulator with no upper bound**, where each call from an unprivileged actor **extends** a shared lock state and can push the effective unlock time arbitrarily far into the future, degrading availability/usability until a privileged actor intervenes.

The closest local analog in `polkadot-sdk` is the `pallet-safe-mode` extend mechanism. `EnteredUntil` is a single global `StorageValue` that gates the entire chain's call filter (`Contains` impl used as `BaseCallFilter`), and it is accrued via `Pallet::extend`, callable by **any signed account**, with **no upper bound** on how many times or how far it can be pushed out. [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
Unbounded permissionless accrual of `EnteredUntil` in `pallet-safe-mode::extend` allows chain-wide call-filter griefing with no cap - (File: `substrate/frame/safe-mode/src/lib.rs`)

### Summary
`Pallet::extend` (and the underlying `do_extend`) lets any signed account, upon reserving `ExtendDepositAmount`, accrue `ExtendDuration` onto the global `EnteredUntil` value with `saturating_accrue`, and the pallet's own documentation states there is intentionally "no hard limit as the safe-mode can be extended multiple times." [4](#0-3)  Because `hold()` only rejects a *repeat* hold from the *same* account (checking `balance_on_hold` for that specific account), a set of distinct signed accounts can each place one `ExtendDepositAmount` deposit and call `extend` once, and each such call unconditionally adds `ExtendDuration` to `EnteredUntil` with no ceiling. [5](#0-4) 

### Finding Description
`EnteredUntil` is the sole state gating `Pallet::is_allowed`, which is wired as (part of) `frame_system::Config::BaseCallFilter` in any runtime that adopts safe-mode; while entered, only calls in `T::WhitelistedCalls` may be dispatched. [6](#0-5) 

`do_extend` reads the current `EnteredUntil`, optionally takes a deposit from the caller, and then does:
```rust
until.saturating_accrue(duration);
EnteredUntil::<T>::put(until);
```
with `duration = T::ExtendDuration::get()` — a fixed runtime constant, not something scaled or capped against a maximum lockout window. [3](#0-2) 

The only guard against repeated extension by the same actor is in `hold`, which errors with `AlreadyDeposited` if the *same* `who` already has a nonzero hold for `HoldReason::EnterOrExtend`. [7](#0-6)  This guard does nothing to bound the number of *distinct* unprivileged accounts that can each place one deposit and call `extend()`, nor does it bound how many times the cycle deposit → extend → wait for `ReleaseDelay` → `release_deposit` → repeat can be performed by the same account over time. There is no maximum value enforced on `EnteredUntil`, unlike the fix applied to the analogous vault bug report (adding a hard cap on `depositRefundTimeout`).

The only way out before the natural `on_initialize` timeout check (`current > limit`) is `force_exit`, gated by `T::ForceExitOrigin` — i.e., recovery requires a privileged/governance action, exactly mirroring the vault report's scenario where the only way out of an over-long lock is an explicit remedial action, and in the meantime the chain-wide filter stays in effect for all unprivileged users.

### Impact Explanation
While safe-mode is entered, only whitelisted calls dispatch; everything else is rejected by the base call filter. An unprivileged, permissionless, and unboundedly repeatable `extend` path lets a set of funded accounts keep pushing `EnteredUntil` forward indefinitely (each deposit is later reclaimable via `release_deposit`/`force_release_deposit`, so the capital cost is only the time-value of the deposit, not a burn). This is a chain-availability degradation: normal (non-whitelisted) extrinsics — including ordinary transfers, staking operations, etc. — are blocked for as long as the attacker is willing to keep cycling deposits, until a privileged `ForceExitOrigin` intervenes. This matches the "public underpriced work that degrades block production or stalls…processing" impact category, since a bounded per-call cost (`ExtendDepositAmount`, fully refundable) buys an unbounded chain-wide functional lockout.

### Likelihood Explanation
Likelihood depends entirely on how a given runtime configures `ExtendDepositAmount`/`ExtendDuration`/`ReleaseDelay`/`ForceExitOrigin`; runtimes that set `ExtendDepositAmount = None` disable this path entirely, and runtimes with a fast, easily-triggered `ForceExitOrigin` (e.g. a low-threshold technical committee) reduce the exposure window. But nothing in the pallet itself enforces a ceiling — the safety property relies entirely on runtime configuration and out-of-band governance responsiveness, which is precisely the "no upper bound" footgun the external report calls out and that Aera patched for their own timeout variable.

### Recommendation
- Enforce a configurable maximum value for `EnteredUntil` (a max cumulative extension length from the original `enter` block), analogous to the fix applied to `depositRefundTimeout` in the referenced report.
- Consider bounding the number of independent extensions accepted within a rolling window, or requiring a monotonically increasing deposit cost per additional extension to make griefing economically infeasible rather than merely capital-locked.
- Document explicitly for runtime integrators that `ExtendDepositAmount` must be configured with economic griefing cost in mind, and that `ForceExitOrigin` should be a fast-acting body.

### Proof of Concept
1. Configure a runtime with `pallet-safe-mode`, `ExtendDepositAmount = Some(D)`, `ExtendDuration = N` blocks, and `EnterDepositAmount = Some(E)`.
2. Attacker (or a colluding party) funds `k` distinct signed accounts, each with at least `D` free balance.
3. One account calls `enter()`, placing `EnteredUntil = now + EnterDuration`.
4. Each of the `k` funded accounts calls `extend()` once: each call succeeds because `hold()` only checks that *that* account has no existing hold, and each call unconditionally does `EnteredUntil.saturating_accrue(N)`.
5. After `k` calls, `EnteredUntil = now + EnterDuration + k*N`, growing linearly and without bound in `k`, entirely from unprivileged signed calls, freezing all non-whitelisted dispatches chain-wide until `force_exit` is invoked by `ForceExitOrigin`. [8](#0-7)

### Citations

**File:** substrate/frame/safe-mode/src/lib.rs (L118-124)
```rust
		type EnterDuration: Get<BlockNumberFor<Self>>;

		/// For how many blocks the safe-mode can be extended by each [`Pallet::extend`] call.
		///
		/// This does not impose a hard limit as the safe-mode can be extended multiple times.
		#[pallet::constant]
		type ExtendDuration: Get<BlockNumberFor<Self>>;
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

**File:** substrate/frame/safe-mode/src/lib.rs (L359-374)
```rust
		/// Exit safe-mode by force.
		///
		/// Emits an [`Event::Exited`] with [`ExitReason::Force`] event on success.
		/// Errors with [`Error::Exited`] if the safe-mode is inactive.
		///
		/// Note: `safe-mode` will be automatically deactivated by [`Pallet::on_initialize`] hook
		/// after the block height is greater than the [`EnteredUntil`] storage item.
		/// Emits an [`Event::Exited`] with [`ExitReason::Timeout`] event when deactivated in the
		/// hook.
		#[pallet::call_index(4)]
		#[pallet::weight(T::WeightInfo::force_exit())]
		pub fn force_exit(origin: OriginFor<T>) -> DispatchResult {
			T::ForceExitOrigin::ensure_origin(origin)?;

			Self::do_exit(ExitReason::Force).map_err(Into::into)
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

**File:** substrate/frame/safe-mode/src/lib.rs (L560-575)
```rust
	/// Place a hold for exactly `amount` and store it in `Deposits`.
	///
	/// Errors if the account already has a hold for the same reason.
	fn hold(who: T::AccountId, amount: BalanceOf<T>) -> Result<(), Error<T>> {
		let block = <frame_system::Pallet<T>>::block_number();
		if !T::Currency::balance_on_hold(&HoldReason::EnterOrExtend.into(), &who).is_zero() {
			return Err(Error::<T>::AlreadyDeposited.into());
		}

		T::Currency::hold(&HoldReason::EnterOrExtend.into(), &who, amount)
			.map_err(|_| Error::<T>::CurrencyError)?;
		Deposits::<T>::insert(&who, block, amount);
		Self::deposit_event(Event::<T>::DepositPlaced { account: who, amount });

		Ok(())
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
