### Title
Crowdloan contributor funds can become permanently locked if `Auctioneer::lease_period_index` returns `None` — ([File: polkadot/runtime/common/src/crowdloan/mod.rs])

### Summary
`pallet-crowdloan` (used in the Westend and Rococo runtimes) gates the two *permissionless* fund-recovery calls, `withdraw` and `refund`, behind `ensure_crowdloan_ended`, which itself depends on `T::Auctioneer::lease_period_index(now)` returning `Some(..)`. If this call returns `None`, both fund-recovery paths permanently fail, and the fallback path (`dissolve`) cannot be used because it additionally requires `fund.raised` to already be zero — a state that can only be reached via the now-blocked `withdraw`/`refund`. This is structurally the same defect as the CoinFabrik `Crowdsale`: a state-exit is nominally covered by "normal" mechanics, but a config/dependency condition (an "agent"-like external actor: the auction/lease system) that is not guaranteed to be set/available prevents the contract from ever leaving its current state, trapping user funds with no alternate recovery route.

### Finding Description
`ensure_crowdloan_ended` is the sole guard used by both `withdraw` (call_index 2) and `refund` (call_index 3): [1](#0-0) 

It requires:
```
let (current_lease_period, _) =
    T::Auctioneer::lease_period_index(now).ok_or(Error::<T>::NoLeasePeriod)?;
```
`lease_period_index` is documented to return `None` "if the first lease period has not started yet, for example when an offset is placed": [2](#0-1) 

If that condition is ever true for a fund whose crowdloan has already ended (`now >= fund.end`), both `withdraw` and `refund` immediately return `Error::<T>::NoLeasePeriod` for every caller, with no other extrinsic able to move `fund.raised` back to zero. `dissolve` (call_index 4), the only other terminal call, explicitly requires `fund.raised.is_zero()`: [3](#0-2) 

This creates a hard circular dependency: `dissolve` needs `raised == 0`, but the only calls that can reduce `raised` (`withdraw`/`refund`) are blocked by the same lease-period precondition. There is no admin/root override in the pallet to force a refund or bypass `ensure_crowdloan_ended`. Contributors' funds, sitting in the fund's sovereign pot account (`fund_account_id`), become permanently unreachable by any signed account.

This mirrors the reported bug class precisely: the code's normal-path comments/design imply contributors "will get a refund... before the crowdloan can be dissolved" (see module doc comment), i.e., refund is assumed always eventually available — but the actual gating condition (`Auctioneer::lease_period_index` returning `Some`) is an external dependency that is not guaranteed to hold, exactly like the CoinFabrik contract's implicit assumption that a `finalizeAgent` would always be supplied.

### Impact Explanation
If triggered, this permanently locks contributor funds (`fund.raised`) inside the crowdloan pot account for that `ParaId`, with no path to recovery for depositor or contributors — a direct, unbacked, permanent user-fund lock, matching the "permanent user-fund... lock" impact category in the program scope.

### Likelihood Explanation
This requires no malicious actor, admin, or governance action — it can occur purely from the ordinary lifecycle/configuration state of the `Auctioneer` implementation (e.g., lease-period offset not yet reached, or auction/lease mechanics being wound down as parachains migrate away from slot auctions to Agile Coretime). I was not able to fully verify, within the available tooling, the exact runtime configuration of `Leases`/`Auctions` in the current Westend/Rococo runtime to determine how easily `lease_period_index` can return `None` for an already-ended fund in production; this would need to be confirmed by tracing the concrete `Auctioneer` implementation used by `pallet-crowdloan::Config::Auctioneer` in those runtimes (e.g., `polkadot/runtime/rococo/src/lib.rs`, `polkadot/runtime/westend/src/lib.rs`), which the current index did not return in enough detail.

### Recommendation
- Decouple `withdraw`/`refund` eligibility from `Auctioneer::lease_period_index`. The lease-period check in `ensure_crowdloan_ended` should not be a hard blocker for refunds once `fund.end` has passed and the pot's free balance covers `fund.raised` — the lease-period-based condition should only matter for determining whether a slot was still being actively contested, not for gating basic fund recovery.
- Add a permissionless or governance-independent fallback (e.g., a time-locked force-refund path) that does not depend on `Auctioneer::lease_period_index` returning `Some`, so contributor funds cannot be trapped by an external dependency going into a state the crowdloan pallet did not anticipate.
- Add explicit invariant/documentation and tests covering the case where `Auctioneer::lease_period_index` returns `None` for a `now >= fund.end` fund, verifying that funds remain recoverable in that state.

### Proof of Concept
1. Create a crowdloan fund via `Crowdloan::create` with `end = E`, referencing an `Auctioneer` implementation (e.g., `pallet-auctions`/`pallet-slots` lease system).
2. Contributors call `contribute`, raising `fund.raised > 0`.
3. Advance the chain past block `E` so the crowdloan should be "ended".
4. Arrange the `Auctioneer::lease_period_index(now)` implementation to return `None` for the current block (e.g., a lease-period offset/reset, or the auction/lease system disabled while migrating to Agile Coretime).
5. Call `Crowdloan::withdraw` or `Crowdloan::refund` — both fail with `Error::<T>::NoLeasePeriod`, from `ensure_crowdloan_ended` at [4](#0-3) .
6. Call `Crowdloan::dissolve` — fails with `Error::<T>::NotReadyToDissolve` because `fund.raised != 0`, per [5](#0-4) .
7. No dispatchable in the pallet can now move `fund.raised` to zero or release the pot's funds — contributor balances remain locked in the fund account indefinitely.

### Citations

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L552-581)
```rust
		/// Remove a fund after the retirement period has ended and all funds have been returned.
		#[pallet::call_index(4)]
		#[pallet::weight(T::WeightInfo::dissolve())]
		pub fn dissolve(origin: OriginFor<T>, #[pallet::compact] index: ParaId) -> DispatchResult {
			let who = ensure_signed(origin)?;

			let fund = Funds::<T>::get(index).ok_or(Error::<T>::InvalidParaId)?;
			let pot = Self::fund_account_id(fund.fund_index);
			let now = frame_system::Pallet::<T>::block_number();

			// Only allow dissolution when the raised funds goes to zero,
			// and the caller is the fund creator or we are past the end date.
			let permitted = who == fund.depositor || now >= fund.end;
			let can_dissolve = permitted && fund.raised.is_zero();
			ensure!(can_dissolve, Error::<T>::NotReadyToDissolve);

			// Assuming state is not corrupted, the child trie should already be cleaned up
			// and all funds in the crowdloan account have been returned. If not, governance
			// can take care of that.
			debug_assert!(Self::contribution_iterator(fund.fund_index).count().is_zero());

			// Crowdloan over, burn all funds.
			let _imba = CurrencyOf::<T>::make_free_balance_be(&pot, Zero::zero());
			let _ = frame_system::Pallet::<T>::dec_providers(&pot).defensive();

			CurrencyOf::<T>::unreserve(&fund.depositor, fund.deposit);
			Funds::<T>::remove(index);
			Self::deposit_event(Event::<T>::Dissolved { para_id: index });
			Ok(())
		}
```

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L721-747)
```rust
	/// This function checks all conditions which would qualify a crowdloan has ended.
	/// * If we have reached the `fund.end` block OR the first lease period the fund is trying to
	///   bid for has started already.
	/// * And, if the fund has enough free funds to refund full raised amount.
	fn ensure_crowdloan_ended(
		now: BlockNumberFor<T>,
		fund_account: &T::AccountId,
		fund: &FundInfo<T::AccountId, BalanceOf<T>, BlockNumberFor<T>, LeasePeriodOf<T>>,
	) -> sp_runtime::DispatchResult {
		// `fund.end` can represent the end of a failed crowdloan or the beginning of retirement
		// If the current lease period is past the first period they are trying to bid for, then
		// it is already too late to win the bid.
		let (current_lease_period, _) =
			T::Auctioneer::lease_period_index(now).ok_or(Error::<T>::NoLeasePeriod)?;
		ensure!(
			now >= fund.end || current_lease_period > fund.first_period,
			Error::<T>::FundNotEnded
		);
		// free balance must greater than or equal amount raised, otherwise funds are being used
		// and a bid or lease must be active.
		ensure!(
			CurrencyOf::<T>::free_balance(&fund_account) >= fund.raised,
			Error::<T>::BidOrLeaseActive
		);

		Ok(())
	}
```

**File:** polkadot/runtime/common/src/traits.rs (L248-252)
```rust
	/// Returns the lease period at `block`, and if this is the first block of a new lease period.
	///
	/// Will return `None` if the first lease period has not started yet, for example when an offset
	/// is placed.
	fn lease_period_index(block: BlockNumber) -> Option<(Self::LeasePeriod, bool)>;
```
