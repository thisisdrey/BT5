## Local Analog Found

### Title
Crowdloan `ensure_crowdloan_ended` guard can be bypassed via a direct transfer to the fund's public sub-account, allowing withdrawal/refund while a bid is still active - (File: `polkadot/runtime/common/src/crowdloan/mod.rs`)

### Summary
The Buyout bug used a live `balanceOf` check (not a value tracked exclusively through the constrained `sellFractions` path) to decide whether a time-gated action could complete, letting an attacker top up the balance out-of-band to force the "end" condition. The same broken pattern exists in `pallet-crowdloan`'s `ensure_crowdloan_ended`, which gates `withdraw`/`refund` on the fund account's live `free_balance` instead of an authoritative "not currently backing an active bid" flag. Anyone can compute the fund's `AccountId` and send it plain currency to satisfy the check while a bid/lease reservation is still outstanding.

### Finding Description
`ensure_crowdloan_ended` is the single guard used by both `withdraw` and `refund` to decide whether contributors may pull their funds out of a crowdloan: [1](#0-0) 

```
fn ensure_crowdloan_ended(...) {
    ...
    // free balance must greater than or equal amount raised, otherwise funds are being used
    // and a bid or lease must be active.
    ensure!(
        CurrencyOf::<T>::free_balance(&fund_account) >= fund.raised,
        Error::<T>::BidOrLeaseActive
    );
    Ok(())
}
```

The comment states the intent explicitly: if `free_balance < raised`, it means part of `raised` is currently *reserved* because the fund account is actively bidding in an auction (`Auctions::handle_bid` reserves currency directly from the bidder, and the crowdloan pallet passes its own fund account as the bidder): [2](#0-1) 

Contributions themselves are ordinary balance transfers into the fund account (not reserves), so under normal operation `total_balance(fund_account) == fund.raised` and any active bid shows up purely as reduced `free_balance` (funds moved to reserved). This is the *only* signal the guard uses to detect "a bid or lease is active" — it never checks an explicit "active bid" flag.

`fund_account_id` is a `pub fn` deriving a deterministic sub-account from `PalletId` + `FundIndex`: [3](#0-2) 

Any signed account (not required to be a contributor, curator, or otherwise privileged) can transfer arbitrary native currency directly to this address using `pallet-balances::transfer` — a completely separate, unconstrained path from `contribute()`/`contribute_all()`, exactly mirroring how the ERC1155 fraction tokens were transferred directly to the `Buyout` contract outside of `sellFractions`.

By sending enough plain currency to `fund_account_id(index)` to cover the currently reserved (bid-backing) amount, an attacker makes `free_balance(fund_account) >= fund.raised` true again even though funds are still reserved for an active bid/lease. `ensure_crowdloan_ended` then incorrectly reports success, and `withdraw`/`refund` proceed to drain contributors' tracked balances out of the fund account while the bid/lease reservation is still live and could still win the slot auction.

### Impact Explanation
This breaks the invariant the guard exists to enforce: contributors must not be able to exit a crowdloan whose funds are still backing an active parachain-slot bid. With the guard defeated:
- Contributors (or a colluding attacker) can withdraw/refund their tracked contribution while the crowdloan's fund account is still reserved as the winning/leading bidder for a slot, undermining the assumption that `raised` matches the funds actually available to back that bid.
- This is a state-machine/accounting integrity violation in a public-dispatch pallet (`withdraw`, `refund` are callable by any signed account "on behalf of" anyone), matching the required impact categories of runtime bugs compromising intended behavior and message/queue-like settlement state advancing before the underlying condition (no active bid) is genuinely true.
- No malicious validator, collator, governance actor, or leaked key is required — a plain signed `transfer` extrinsic to a publicly-computable account is sufficient.

### Likelihood Explanation
`fund_account_id` is a pure, deterministic function of public inputs (`PalletId`, `FundIndex`), so any observer can compute the target account for any live crowdloan. Triggering the bypass only requires a normal `balances::transfer` call followed by `withdraw`/`refund` — both permissionless, single-transaction operations with no special timing beyond "a bid is currently active," which is itself observable on-chain.

### Recommendation
Do not infer "no active bid" from the fund account's live `free_balance`. Track the reserved/backing amount explicitly (e.g., record `ReservedAmounts` state per fund, mirroring what `pallet-auctions` already tracks per bidder/para) and have `ensure_crowdloan_ended` check that tracked value directly, independent of any balance that may have been transferred into the account out-of-band.

### Proof of Concept
1. Fund X is created and several users `contribute()`, raising `fund.raised = 1000`; `fund_account_id(X)` now holds `total_balance == 1000`, all free.
2. Fund X's `NewRaise` bid logic (via `on_initialize` → `Auctioneer::place_bid`) causes `pallet-auctions` to `reserve(&fund_account, 800)` from the fund account as part of an active/leading bid. Now `free_balance(fund_account) = 200 < raised (1000)`, so `ensure_crowdloan_ended` correctly returns `BidOrLeaseActive` for any `withdraw`/`refund` call.
3. Attacker (any signed account, not necessarily a contributor) calls `Balances::transfer(fund_account_id(X), 800)`. Now `free_balance(fund_account) = 1000 >= raised (1000)`.
4. A contributor calls `Crowdloan::withdraw(who, X)` (or anyone calls `refund(X)`); `ensure_crowdloan_ended` now passes despite the bid still being active/reserved, and contributions are paid out of the fund account's free balance while the 800 remains reserved for the still-live bid.


In repository ThankGodontt/polkadot-sdk--036, file `polkadot/runtime/common/src/crowdloan/mod.rs`:

Context: `ensure_crowdloan_ended` (used by the public dispatchables `withdraw` and `refund`) determines whether a crowdloan fund is safe to pay out by comparing `CurrencyOf::<T>::free_balance(&fund_account) >= fund.raised`. This is meant to detect "a bid or lease is active" (in which case part of `raised` is reserved by `pallet-auctions` against the fund account as bidder), but it relies purely on the live queryable balance of a deterministic, publicly-computable sub-account (`Pallet::<T>::fund_account_id`). Anyone can send ordinary `pallet-balances::transfer` currency directly to that account to artificially inflate `free_balance` and make the check pass even while a bid/lease reservation against the fund account is still outstanding, letting `withdraw`/`refund` pay out contributor funds prematurely while the fund is still committed to backing an active parachain slot bid/lease.

Task:
1. Add explicit state tracking of whether/how much of a fund's `raised` balance is currently reserved/committed to an active bid or lease, instead of inferring it from `free_balance(&fund_account)`. This could be a new storage item or field on `FundInfo` (e.g., `active_bid_amount` or similar) updated whenever `pallet-auctions`/`Auctioneer` reserves or unreserves against the fund account, or alternatively query `pallet-balances::reserved_balance(&fund_account)` directly and compare/reconcile with the actual expected reserved amount rather than only checking that free balance covers `raised`.
2. Update `ensure_crowdloan_ended` in `polkadot/runtime/common/src/crowdloan/mod.rs` to use this authoritative tracked value instead of (or in addition to, with reconciliation) the raw `free_balance` check, so that a direct/out-of-band transfer to the fund account cannot cause the guard to pass while a bid/lease reservation is genuinely still active.
3. Add regression tests in `polkadot/runtime/common/src/crowdloan/mod.rs` (or `polkadot/runtime/common/src/integration_tests.rs`) that: (a) start a crowdloan, have it place/win an active bid so part of its balance is reserved, (b) directly transfer extra native currency to the fund's account via `Balances::transfer`, and (c) assert that `withdraw`/`refund` still correctly fail with `Error::BidOrLeaseActive` (or equivalent) despite the inflated free balance, confirming the fix closes the bypass while preserving legitimate withdrawal/refund behavior once the bid/lease is genuinely no longer active.

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

**File:** polkadot/runtime/common/src/auctions/mod.rs (L463-483)
```rust
		// If this bid beat the previous winner of our range.
		if current_winning[range_index].as_ref().map_or(true, |last| amount > last.2) {
			// Ok; we are the new winner of this range - reserve the additional amount and record.

			// Get the amount already held on deposit if this is a renewal bid (i.e. there's
			// an existing lease on the same para by the same leaser).
			let existing_lease_deposit = T::Leaser::deposit_held(para, &bidder);
			let reserve_required = amount.saturating_sub(existing_lease_deposit);

			// Get the amount already reserved in any prior and still active bids by us.
			let bidder_para = (bidder.clone(), para);
			let already_reserved = ReservedAmounts::<T>::get(&bidder_para).unwrap_or_default();

			// If these don't already cover the bid...
			if let Some(additional) = reserve_required.checked_sub(&already_reserved) {
				// ...then reserve some more funds from their account, failing if there's not
				// enough funds.
				CurrencyOf::<T>::reserve(&bidder, additional)?;
				// ...and record the amount reserved.
				ReservedAmounts::<T>::insert(&bidder_para, reserve_required);

```
