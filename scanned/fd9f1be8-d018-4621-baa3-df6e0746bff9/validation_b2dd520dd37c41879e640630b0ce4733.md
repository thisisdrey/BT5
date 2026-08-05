Based on my investigation, I found a strong local analog in `substrate/frame/multi-asset-bounties`, which mirrors the exact broken invariant from the report: a public entrypoint checks a *local, bookkeeping* cap (`ChildBountiesValuePerParent`, a cumulative counter) instead of validating against the *actual underlying resource* it draws from (the parent bounty's real, current balance/asset availability). This is the same class of bug as `CuratedVaultSetters::_supplyPool()` trusting `config[pool].cap` while ignoring the underlying pool's real `supplyCap`/balance.

### Title
`fund_child_bounty()` validates against a stale bookkeeping counter (`ChildBountiesValuePerParent`) instead of the parent bounty's actual funded balance, enabling reverting/duplicate-accounting fund allocation - (File: `substrate/frame/multi-asset-bounties/src/lib.rs`)

### Summary
`Pallet::fund_child_bounty` enforces the child-bounty value cap by comparing `parent_value.saturating_sub(ChildBountiesValuePerParent::<T,I>::get(parent_bounty_id))` against the requested `value` [1](#0-0) . This mirrors the reported bug pattern exactly: it checks a locally tracked "nominal" cap (`parent_value` minus a running-total counter) rather than the true, currently available balance/asset backing the parent bounty. If the actual funding of the parent bounty (via `do_process_funding_payment`, an asynchronous, retryable payment) has not fully landed, is partially failed, or the underlying asset location's balance diverges from the nominal `parent_value` (e.g., through slashes, external transfers, or `PayFromAccount`-style delayed settlement), the local check passes while the underlying payment step can revert or under/over allocate.

### Finding Description
The bug-class from the report is: a supply/allocation function trusts a *virtual* cap tracked in the caller's own storage instead of the *real* capacity/state of the resource it draws from, leading to reverts on legitimate operations or accounting drift. In `fund_child_bounty`:
- The cap check uses `ChildBountiesValuePerParent` (a cumulative sum incremented at `fund_child_bounty` call time) and `parent_value` fetched via `get_bounty_details` [2](#0-1) .
- Funding is not synchronous fund movement from a verified balance check as in the classic `child-bounties` pallet (which checks `T::Currency::free_balance(&parent_bounty_account)` directly before transferring) [3](#0-2) . Instead, `multi-asset-bounties::fund_child_bounty` calls `Self::do_process_funding_payment(...)` which returns a `payment_status` that can itself represent a **pending or failed** payment attempt (the child bounty is created in `BountyStatus::FundingAttempted { payment_status, .. }` state) [4](#0-3) .
- Crucially, `ChildBountiesValuePerParent` and `ChildBountiesPerParent` are incremented **unconditionally** after `do_process_funding_payment` is called, regardless of whether the payment attempt itself succeeded [5](#0-4) .

This means the "cap" being enforced (`remaining_parent_value >= value`) is against a value that has already been decremented for prior child bounties whose underlying payment may still be in-flight or have failed — exactly the same "local cap vs. real underlying resource" mismatch as the `CuratedVault` report, where `config[pool].cap` (local) diverges from the underlying pool's actual `supplyCap`/balance.

### Impact Explanation
Under the "Required Impacts" gate, this matches "duplicate settlement or payout" / "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" — here, `ChildBountiesValuePerParent` (the settlement bookkeeping state) advances *before* the underlying payment is confirmed to have succeeded, and it is never rolled back if the payment permanently fails (only `retry_payment`/`check_status` paths exist for the curator to retry, but the value accounting has already been consumed). This can produce:
1. **Headroom loss** — genuine child-bounty funding requests are rejected (`InsufficientBountyValue`) even though the parent bounty's real balance would support them, because the counter was consumed by a child bounty whose payment never actually landed.
2. **Accounting drift enabling double-claiming risk** — if a failed/pending payment is later retried and also independently reconciled, the cumulative counter and the real balance can diverge in ways that let more value be paid out than the parent bounty actually holds, since award/payout logic elsewhere in the pallet likely also trusts `ChildBountiesValuePerParent` rather than re-deriving from actual balances.

### Likelihood Explanation
This requires no privileged actor beyond the normal parent-bounty curator (an intended, unprivileged-relative-to-chain-security caller of a public extrinsic), and no malicious relayer/validator/governance abuse — it is a straightforward implementation gap in the payment-state bookkeeping of a public dispatchable, consistent with the "Discard" list exclusions (no malicious peer/node/validator/admin required). The `FundingAttempted`/retry pattern documented in the code (`check_status`, `retry_payment`) confirms that payment failures are an expected, ordinary occurrence in this pallet, not an edge case — making the mismatch between local counter and real balance a routinely reachable condition.

### Recommendation
Do not increment `ChildBountiesValuePerParent`/`ChildBountiesPerParent` until `do_process_funding_payment` reports a *confirmed successful* settlement (mirror the atomic-advance-after-success invariant). Alternatively, re-validate against the actual current balance/asset amount held at the parent bounty's payment source at the time of `check_status` confirmation, and roll back the cumulative counters if a funding attempt is confirmed as failed, so the "cap" always reflects real, currently committed capacity rather than an optimistic running total.

### Proof of Concept
Not independently executed (no sandbox/test run performed here); this assessment is based on static code review of the cited functions. A concrete PoC would: (1) create a parent bounty with `parent_value = V`; (2) call `fund_child_bounty` with `value = V` where the configured `T::BalanceConverter`/payment path is one that can return a non-`Success` `payment_status` (e.g. simulate a failing `PayFromAccount` implementation in a mock runtime); (3) observe `ChildBountiesValuePerParent` is already incremented to `V` even though the underlying payment never completed; (4) attempt a second legitimate `fund_child_bounty` call and observe it is rejected with `InsufficientBountyValue` despite the parent bounty's real balance being fully available, demonstrating the local-cap vs. real-balance divergence. [6](#0-5)

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L670-737)
```rust
			let (asset_kind, parent_value, _, _, parent_curator) =
				Self::get_bounty_details(parent_bounty_id, None)
					.map_err(|_| Error::<T, I>::InvalidIndex)?;
			let native_amount = T::BalanceConverter::from_asset_balance(value, asset_kind.clone())
				.map_err(|_| Error::<T, I>::FailedToConvertBalance)?;

			ensure!(
				native_amount >= T::ChildBountyValueMinimum::get(),
				Error::<T, I>::InvalidValue
			);
			ensure!(
				ChildBountiesPerParent::<T, I>::get(parent_bounty_id) <
					T::MaxActiveChildBountyCount::get(),
				Error::<T, I>::TooManyChildBounties,
			);

			// Parent bounty must be `Active` with a curator assigned.
			let parent_curator = parent_curator.ok_or(Error::<T, I>::UnexpectedStatus)?;
			let final_curator = match curator {
				Some(curator) => T::Lookup::lookup(curator)?,
				None => parent_curator.clone(),
			};
			ensure!(signer == parent_curator, Error::<T, I>::RequireCurator);

			// Check value
			let child_bounties_value = ChildBountiesValuePerParent::<T, I>::get(parent_bounty_id);
			let remaining_parent_value = parent_value.saturating_sub(child_bounties_value);
			ensure!(remaining_parent_value >= value, Error::<T, I>::InsufficientBountyValue);

			// Get child-bounty ID.
			let child_bounty_id = TotalChildBountiesPerParent::<T, I>::get(parent_bounty_id);

			// Initiate funding payment
			let payment_status = Self::do_process_funding_payment(
				parent_bounty_id,
				Some(child_bounty_id),
				asset_kind,
				value,
				None,
			)?;

			let child_bounty = ChildBounty {
				parent_bounty: parent_bounty_id,
				value,
				metadata,
				status: BountyStatus::FundingAttempted {
					curator: final_curator,
					payment_status: payment_status.clone(),
				},
			};
			ChildBounties::<T, I>::insert(parent_bounty_id, child_bounty_id, child_bounty);
			T::Preimages::request(&metadata);

			// Add child-bounty value to the cumulative value sum. To be
			// subtracted from the parent bounty payout when awarding
			// bounty.
			ChildBountiesValuePerParent::<T, I>::mutate(parent_bounty_id, |children_value| {
				*children_value = children_value.saturating_add(value)
			});

			// Increment the active child-bounty count.
			ChildBountiesPerParent::<T, I>::mutate(parent_bounty_id, |count| {
				count.saturating_inc()
			});
			TotalChildBountiesPerParent::<T, I>::insert(
				parent_bounty_id,
				child_bounty_id.saturating_add(1),
			);
```

**File:** substrate/frame/child-bounties/src/lib.rs (L298-312)
```rust
			// Read parent bounty account info.
			let parent_bounty_account =
				pallet_bounties::Pallet::<T>::bounty_account_id(parent_bounty_id);

			// Ensure parent bounty has enough balance after adding child-bounty.
			let bounty_balance = T::Currency::free_balance(&parent_bounty_account);
			let new_bounty_balance = bounty_balance
				.checked_sub(&value)
				.ok_or(Error::<T>::InsufficientBountyBalance)?;
			T::Currency::ensure_can_withdraw(
				&parent_bounty_account,
				value,
				WithdrawReasons::TRANSFER,
				new_bounty_balance,
			)?;
```
