### Title
Crowdloan pot balance-based "ended" check can be spoofed by direct donation, letting withdraw/refund bypass an active bid/lease guard - (File: `polkadot/runtime/common/src/crowdloan/mod.rs`)

### Summary
`pallet-crowdloan`'s `ensure_crowdloan_ended` treats the *actual* free balance held in the fund's pot account as a proxy for "no bid or lease is currently active," instead of relying on a value the runtime itself tracks and controls. This is the same bug class as the reported Solana `price-lock` issue: substituting a raw, externally-influenceable account balance for a properly computed/tracked accounting value used to gate a critical state transition.

### Finding Description
`ensure_crowdloan_ended` is called from both the public `withdraw` and `refund` extrinsics before allowing contributor funds to leave the crowdloan pot: [1](#0-0) 

```rust
fn ensure_crowdloan_ended(
    now: BlockNumberFor<T>,
    fund_account: &T::AccountId,
    fund: &FundInfo<T::AccountId, BalanceOf<T>, BlockNumberFor<T>, LeasePeriodOf<T>>,
) -> sp_runtime::DispatchResult {
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

The comment makes the intent explicit: when a crowdloan wins an auction/bid, `slots`/`auctions` logic reserves part of the pot's balance (`Balances::reserve`), so `free_balance(pot) < fund.raised` while the bid/lease is active, and this is the *only* signal `withdraw`/`refund` use to detect that condition. The fund pot account (`fund_account_id`) is a deterministic `PalletId` sub-account derived from a public `fund_index`, and it is an ordinary `AccountId` — nothing in `pallet-balances` prevents an arbitrary unprivileged signed account from calling `transfer`/`transfer_keep_alive` to send extra free tokens directly into that pot.

Because the guard compares raw free balance against `fund.raised` rather than checking a runtime-tracked reservation/hold amount, an attacker who tops up the pot's *free* balance can push `free_balance(pot)` back above `fund.raised` even while a bid/lease genuinely reserves part of the pot. The check passes, `Error::BidOrLeaseActive` is never raised, and `withdraw`/`refund` proceed to transfer contributor balances out of the pot via `CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)`: [2](#0-1) [3](#0-2) 

There is no other guard that independently verifies the parachain lease/auction status of `index` inside `withdraw`/`refund` — the entire "is a bid or lease active" check is delegated to this single free-balance comparison, so satisfying it with donated funds is sufficient to bypass the intended protection.

### Impact Explanation
If the free-balance check can be defeated while a fund is genuinely backing an active auction bid or an active parachain lease, contributors (or anyone triggering `refund`) can drain the crowdloan pot's remaining contributions while the parachain slot is still supposed to be secured by those funds. This directly threatens fund-accounting invariants the SDK Impact Gate cares about (conserve value / settle exactly once): the same raised funds could be both backing a live lease/bid and refunded to contributors, corrupting `fund.raised` bookkeeping relative to what is actually reserved for the slot, and potentially leaving the crowdloan pot under-funded relative to its outstanding lease obligation. This is reachable by any unprivileged account (anyone can `transfer` tokens to the deterministic pot address, and `withdraw`/`refund` are permissionlessly callable by design), matching the gate's "unauthorized execution / wrong beneficiary or amount / duplicate settlement" impact class, not a privileged-admin or malicious-validator scenario.

### Likelihood Explanation
Exploiting this requires: (1) computing the deterministic `fund_account_id` (public, on-chain derivable via `PalletId::into_sub_account_truncating`), (2) sending a plain balance transfer to that account (no permission needed), and (3) calling the already-public `withdraw`/`refund` extrinsics. All of the pieces (fund index, `fund.raised`, current pot balance) are public on-chain state, so an attacker can precisely compute the minimal donation needed to flip the inequality. The main uncertainty is the exact reservation mechanics used by the currently-linked `Auctioneer`/`slots` implementation for a specific runtime configuration (i.e., whether a real, currently active auction winner's reserved amount plus the donation could still leave the pot in a state where downstream slot/lease accounting is actually corrupted, versus merely bypassing a defensive check that has no further consequence). I was not able to fully trace the `slots`/`auctions` reservation and lease-deposit-return code paths in this session to confirm the full end-to-end fund-loss consequence beyond the withdraw/refund guard bypass itself.

### Recommendation
Do not use the pot's raw `free_balance` as the source of truth for "is a bid or lease active." Instead, query the `Auctioneer`/registrar/slots interfaces directly for the fund's para (e.g., an explicit `has_won_an_auction`/lease-active check, or a value tracked in `FundInfo`/a dedicated storage item updated atomically when a bid is placed or a lease starts/ends) so that donated balance cannot influence the check. If a balance-based check must remain, compare against the actual `reserved_balance`/holds on the pot account rather than inferring reservation state from an inequality on `free_balance` that can be inflated by external transfers.

### Proof of Concept
1. Create crowdloan for `index`, have it contribute enough to raise `fund.raised = R` and win an auction, causing the `Auctioneer` implementation to reserve some amount `X <= R` of the pot's balance (so `free_balance(pot) = R - X < R`).
2. While the lease/bid is still active, have any unprivileged account send a plain `Balances::transfer(pot_account, X)` (or more) to the pot's deterministic `fund_account_id(fund_index)`.
3. Now `free_balance(pot) = (R - X) + X = R >= fund.raised`, satisfying `ensure_crowdloan_ended`'s check even though the lease/bid is still active.
4. Call `crowdloan::withdraw` (or `refund`) for a contributor; the call succeeds, transferring the contributor's `balance` out of the pot, despite the fund still backing an active slot.
5. Compare `fund.raised` bookkeeping and actual remaining pot/lease commitments afterward to show the accounting no longer reflects a fund that is either fully active or fully wound down — the guard meant to prevent this (`Error::BidOrLeaseActive`) never fired.

### Citations

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L475-500)
```rust
		pub fn withdraw(
			origin: OriginFor<T>,
			who: T::AccountId,
			#[pallet::compact] index: ParaId,
		) -> DispatchResult {
			ensure_signed(origin)?;

			let mut fund = Funds::<T>::get(index).ok_or(Error::<T>::InvalidParaId)?;
			let now = frame_system::Pallet::<T>::block_number();
			let fund_account = Self::fund_account_id(fund.fund_index);
			Self::ensure_crowdloan_ended(now, &fund_account, &fund)?;

			let (balance, _) = Self::contribution_get(fund.fund_index, &who);
			ensure!(balance > Zero::zero(), Error::<T>::NoContributions);

			CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)?;
			CurrencyOf::<T>::reactivate(balance);

			Self::contribution_kill(fund.fund_index, &who);
			fund.raised = fund.raised.saturating_sub(balance);

			Funds::<T>::insert(index, &fund);

			Self::deposit_event(Event::<T>::Withdrew { who, fund_index: index, amount: balance });
			Ok(())
		}
```

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L507-550)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::refund(T::RemoveKeysLimit::get()))]
		pub fn refund(
			origin: OriginFor<T>,
			#[pallet::compact] index: ParaId,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			let mut fund = Funds::<T>::get(index).ok_or(Error::<T>::InvalidParaId)?;
			let now = frame_system::Pallet::<T>::block_number();
			let fund_account = Self::fund_account_id(fund.fund_index);
			Self::ensure_crowdloan_ended(now, &fund_account, &fund)?;

			let mut refund_count = 0u32;
			// Try killing the crowdloan child trie
			let contributions = Self::contribution_iterator(fund.fund_index);
			// Assume everyone will be refunded.
			let mut all_refunded = true;
			for (who, (balance, _)) in contributions {
				if refund_count >= T::RemoveKeysLimit::get() {
					// Not everyone was able to be refunded this time around.
					all_refunded = false;
					break;
				}
				CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)?;
				CurrencyOf::<T>::reactivate(balance);
				Self::contribution_kill(fund.fund_index, &who);
				fund.raised = fund.raised.saturating_sub(balance);
				refund_count += 1;
			}

			// Save the changes.
			Funds::<T>::insert(index, &fund);

			if all_refunded {
				Self::deposit_event(Event::<T>::AllRefunded { para_id: index });
				// Refund for unused refund count.
				Ok(Some(T::WeightInfo::refund(refund_count)).into())
			} else {
				Self::deposit_event(Event::<T>::PartiallyRefunded { para_id: index });
				// No weight to refund since we did not finish the loop.
				Ok(().into())
			}
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
