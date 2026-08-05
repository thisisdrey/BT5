Audit Report

## Title
Crowdloan `ensure_crowdloan_ended` guard can be bypassed via a direct transfer to the fund's public sub-account, allowing withdrawal/refund while a bid is still active - (File: `polkadot/runtime/common/src/crowdloan/mod.rs`)

## Summary
`ensure_crowdloan_ended` infers "no active bid/lease" purely from `CurrencyOf::<T>::free_balance(&fund_account) >= fund.raised`, rather than from an authoritative tracked reservation amount. Since `fund_account_id` is a deterministic function of public inputs (`PalletId` + `FundIndex`), any signed account can send a plain `pallet-balances::transfer` to the fund's sub-account to artificially inflate its free balance and satisfy the check even while `pallet-auctions` still holds a live reservation against that same account for an active bid.

## Finding Description
`ensure_crowdloan_ended` is the sole guard shared by `withdraw` and `refund` to determine that a crowdloan's funds are not currently backing a bid: [1](#0-0) 

The comment makes the intended invariant explicit: `free_balance < raised` is meant to signal reserved-for-bid funds, but this is inferred rather than tracked. `fund_account_id` is a public, pure derivation: [2](#0-1) 

Contributions are ordinary transfers into this account (`CurrencyOf::<T>::transfer(&who, &fund_account, value, existence)`), and `pallet-auctions` reserves currency directly from the fund account (as bidder) when it wins/leads a bid range: [3](#0-2) 

Because the account is fully public and computable, any unprivileged signed account can call `Balances::transfer` to top up the fund account's free balance out-of-band, defeating the free-balance-based inference and causing `ensure_crowdloan_ended` to pass while a genuine bid/lease reservation is still outstanding against the same account.

## Impact Explanation
This is a state-machine/accounting integrity issue in a publicly-dispatchable pallet: `withdraw`/`refund` could pay out contributors' tracked balances from the fund account's free balance while the account is still committed as an active bidder for a parachain slot, corrupting the intended invariant that `raised` funds fully back any active bid until it concludes. This matches the impact-gate category of "runtime bugs that compromise intended behavior" tied to settlement state advancing before the underlying condition is genuinely satisfied.

## Likelihood Explanation
The exploit requires only a standard signed `Balances::transfer` call to a deterministically computable account, followed by a standard `withdraw`/`refund` call — both permissionless single-transaction operations, with the only precondition being an observable, currently-active bid/lease on-chain.

## Recommendation
Track the reserved/backing amount for each fund explicitly (e.g., a per-fund `active_bid_amount`/reserved tracker synchronized with `pallet-auctions`' `ReservedAmounts`), and have `ensure_crowdloan_ended` compare against that authoritative value instead of relying solely on `free_balance`.

## Proof of Concept
1. Fund X raises `fund.raised = 1000`, so `fund_account_id(X)` holds `total_balance == 1000`, fully free.
2. `pallet-auctions` reserves 800 from the fund account for an active/leading bid; `free_balance = 200 < 1000`, so `ensure_crowdloan_ended` correctly rejects `withdraw`/`refund` with `BidOrLeaseActive`.
3. An arbitrary signed account calls `Balances::transfer(fund_account_id(X), 800)`; now `free_balance = 1000 >= raised`.
4. `withdraw`/`refund` is called and `ensure_crowdloan_ended` incorrectly passes despite the 800 still being reserved for the live bid, allowing contributor payout while the bid is still active.

### Citations

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L672-679)
```rust
impl<T: Config> Pallet<T> {
	/// The account ID of the fund pot.
	///
	/// This actually does computation. If you need to keep using it, then make sure you cache the
	/// value and only call this once.
	pub fn fund_account_id(index: FundIndex) -> T::AccountId {
		T::PalletId::get().into_sub_account_truncating(index)
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

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L793-797)
```rust
		CurrencyOf::<T>::transfer(&who, &fund_account, value, existence)?;
		CurrencyOf::<T>::deactivate(value);

		let balance = old_balance.saturating_add(value);
		Self::contribution_put(fund.fund_index, &who, &balance, &memo);
```
