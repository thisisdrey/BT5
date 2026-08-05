## Analog Identified

The `StakingRewards.setRewardsDuration` bug is fundamentally about a permissionless call (`distribute`/`notifyRewardAmount`) that **repeatedly extends a "finish" timestamp with no upper bound**, which in turn perpetually blocks a state transition (changing `rewardsDuration`) that is only permitted once that timestamp is reached. The strongest local analog in `polkadot-sdk` is `pallet-safe-mode`'s permissionless `extend` call, which extends `EnteredUntil` with no hard cap, keeping the chain-wide call filter active and blocking all non-whitelisted extrinsics indefinitely at the cost of a returnable deposit rather than a privileged action. [1](#0-0) [2](#0-1) [3](#0-2) 

### Title
Unbounded permissionless `extend` in `pallet-safe-mode` allows indefinite chain-wide call filtering (analog of unbounded `periodFinish` renewal) - (File: `substrate/frame/safe-mode/src/lib.rs`)

### Summary
`Pallet::extend` is a permissionless, signed-origin extrinsic that pushes `EnteredUntil` further into the future by `T::ExtendDuration` every time it succeeds, with the pallet documentation explicitly stating "This does not impose a hard limit as the safe-mode can be extended multiple times." [1](#0-0)  While safe-mode is entered, `is_allowed`/`Contains::contains` filters out every call not in `T::WhitelistedCalls`, so the entire non-whitelisted surface of the chain (transfers, staking, governance execution, etc.) is blocked until `EnteredUntil` lapses or a privileged `force_exit` is issued. [4](#0-3)  This mirrors the reported bug's core invariant break: an unprivileged, permissionless action perpetually renews a deadline that gates normal operation, with no cap analogous to `setRewardsDuration`'s `periodFinish` gate.

### Finding Description
`do_extend` simply reads `EnteredUntil`, requires it to exist (safe-mode entered), optionally takes a deposit hold from the caller, and unconditionally accrues `duration` onto `until`: [3](#0-2) . There is no maximum total extension, no rate limit tied to a shrinking budget, and no requirement that the caller be the same account that entered/extended previously — any signed account with `T::ExtendDepositAmount` free balance to hold can call `extend` and push the exit block forward again. `hold()` only prevents the *same* account from holding two overlapping deposits at once (`Error::AlreadyDeposited`) [5](#0-4) , so an attacker with several accounts (or waiting for release windows) can keep the filter engaged indefinitely, exactly like the report's "workaround leads to non-smooth... but the underlying gate can be permissionlessly re-armed" pattern.

The only counter-measure is the privileged `T::ForceExitOrigin::force_exit` [6](#0-5)  — a governance-level escape hatch analogous to Maker's acknowledged workaround (`setRewardsDistribution(address(0))` then wait), not a systemic guard baked into `extend` itself.

### Impact Explanation
While safe-mode is active, `frame_system::Config::BaseCallFilter` (wired to `Pallet::is_allowed`) blocks dispatch of any call outside `WhitelistedCalls`. An unprivileged actor who can sustain the deposit requirement (which is held, not burned, and eventually returned) can keep the chain in this restricted state indefinitely by repeatedly calling `extend` right before `EnteredUntil` lapses, denying ordinary users access to non-whitelisted pallets/extrinsics (transfers, staking exits, etc.) for as long as the attacker is willing to lock capital — a public, underpriced-relative-to-impact action that stalls normal chain processing without needing any admin, validator, or relayer compromise.

### Likelihood Explanation
Likelihood depends entirely on runtime configuration: `T::ExtendDepositAmount` must be `Some(_)` (permissionless extend enabled) for this path to exist; it is `None` by default per the doc comment ("a sane default") [7](#0-6) . Any deployment that enables permissionless `extend` (as the pallet is explicitly designed to support) inherits this unbounded-renewal property, and the attack cost is only locked (not spent) capital, making it economically feasible for well-resourced attackers, similar to how the Maker report's issue only manifests once `VestedRewardsDistribution` is actively used.

### Recommendation
Introduce an upper bound on cumulative extension (e.g., a maximum total `EnteredUntil` horizon from the original `enter`, or a maximum number/aggregate duration of `extend` calls), and/or require the extension deposit to scale with elapsed extensions so indefinite renewal becomes economically prohibitive. Alternatively, document and default-disable permissionless `extend` in production configs, and ensure `ForceExitOrigin` is always cheaply reachable so governance can override runaway renewal quickly — mirroring the recommendation to add explicit rebasing/cap logic instead of relying on an unconditionally-renewable gate.

### Proof of Concept
1. Deploy a runtime with `pallet_safe_mode::Config::ExtendDepositAmount = Some(D)` and `ExtendDuration = N` blocks.
2. Attacker (or colluding accounts) calls `enter()` then repeatedly calls `extend()` from fresh accounts (each holding `D`) just before `EnteredUntil` is reached, per `do_extend`'s unconditional `until.saturating_accrue(duration)` [8](#0-7) .
3. As long as extensions keep arriving before expiry, `Pallet::is_allowed` keeps rejecting all non-whitelisted calls [9](#0-8) , indefinitely denying ordinary users access to non-whitelisted functionality without any admin/governance/validator action, only reversible via `ForceExitOrigin::force_exit`.

### Citations

**File:** substrate/frame/safe-mode/src/lib.rs (L120-124)
```rust
		/// For how many blocks the safe-mode can be extended by each [`Pallet::extend`] call.
		///
		/// This does not impose a hard limit as the safe-mode can be extended multiple times.
		#[pallet::constant]
		type ExtendDuration: Get<BlockNumberFor<Self>>;
```

**File:** substrate/frame/safe-mode/src/lib.rs (L132-136)
```rust
		/// The amount that will be reserved upon calling [`Pallet::extend`].
		///
		/// `None` disallows permissionlessly extending the safe-mode and is a sane default.
		#[pallet::constant]
		type ExtendDepositAmount: Get<Option<BalanceOf<Self>>>;
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
