Based on the evidence gathered, the `pallet-multi-asset-bounties` parent/child bounty design contains a real structural analog to the Notional Exponent bug: a parent's funds become permanently inaccessible if a *single* child bounty's payment gets stuck, exactly mirroring "multiple withdraw requests, one blocks all."

### Title
Parent bounty funds permanently locked if a single child bounty's refund/payout payment perpetually fails - (File: substrate/frame/multi-asset-bounties/src/lib.rs)

### Summary
`pallet-multi-asset-bounties` lets a parent bounty be split into child bounties, each tracked independently with its own `BountyStatus` state machine (`FundingAttempted`, `RefundAttempted`, `PayoutAttempted`) driven by an external `Paymaster::pay` call [1](#0-0) . `close_bounty` for a parent bounty is only allowed when `ChildBountiesPerParent::<T, I>::get(parent_bounty_id) == 0` [2](#0-1) . That counter is only decremented when a child bounty is fully removed via `remove_bounty`, which itself only happens when `check_status` observes `PaymentState::Succeeded` for the child's refund or payout [3](#0-2) . If the external `Paymaster` keeps returning `PaymentState::Failed`/`Pending` for that one child (e.g., beneficiary is an account that structurally cannot receive the asset, or the underlying XCM/asset-transfer beneficiary is unreachable), the child bounty can never reach `Succeeded`, so it is never removed, `ChildBountiesPerParent` never reaches zero, and the parent's `close_bounty` permanently reverts with `HasActiveChildBounty` — locking the parent bounty's entire remaining fund balance, not just the stuck child's share.

### Finding Description
The root cause is identical to the external report's: a container of multiple sub-requests (child bounties/withdraw requests) requires *all* of them to independently reach a terminal-success state before the aggregate/parent resource can be settled or reclaimed, and there is no mechanism to isolate or write off a single permanently-failing sub-request. `award_bounty` and `close_bounty` for a child bounty only ever move state forward through `do_process_payout_payment`/`do_process_refund_payment`, both of which call `<T as Config<I>>::Paymaster::pay(...)` and store a `PaymentState::Attempted { id }` [4](#0-3) . Recovery from a failed/stuck payment relies solely on `retry_payment`, which simply re-invokes the same `Paymaster::pay` [5](#0-4)  — there is no code path to forcibly drop, write off, or bypass a permanently-stuck child bounty so the parent can be closed and its funds recovered. `close_bounty` for the parent explicitly gates on `ChildBountiesPerParent::<T, I>::get(parent_bounty_id) == 0` with no override [2](#0-1) .

### Impact Explanation
Because the parent bounty account holds the aggregate value for all its children (funds are transferred from parent to child account on `fund_child_bounty`, and the parent cannot be closed/reclaimed while any child remains), a single child bounty whose beneficiary or payment rail is permanently broken (attacker-chosen beneficiary account that cannot receive the asset, e.g. below existential deposit for a non-sufficient asset, or a foreign/XCM location that always errors) freezes the parent bounty's un-disbursed funds indefinitely. This matches the "Required Impact" of permanent user-fund lock through a public entry point (`fund_child_bounty`/`award_bounty`/`close_bounty` are called by ordinary curators, not privileged governance), with no admin bypass available in this pallet.

### Likelihood Explanation
Likely under normal operation: any curator (a role obtainable without privileged governance action once a bounty is funded and a curator proposal is accepted) can create a child bounty and award it to, or request a refund toward, a beneficiary/location that is guaranteed to fail payment (e.g., an account address that never existed, or is deliberately below ED for a non-sufficient asset). This requires no malicious validator, relayer, or governance actor — only an unprivileged curator/beneficiary choice, matching the disallowed root-cause list's exclusions and staying within the "public entrypoint causing fund lock" acceptance criteria.

### Recommendation
Add a permissionless or governance "reclaim"/"force settle" path analogous to `reclaim_bounty_funds` in `pallet-bounties` (which was added specifically to fix stranded-fund locks) but scoped to child bounties: allow a parent bounty (or `RejectOrigin`) to forcibly write off a child bounty that has been stuck in `RefundAttempted`/`PayoutAttempted` for longer than some grace period, decrementing `ChildBountiesPerParent` and sweeping any residual child-account balance back to the parent/treasury, so a single unrecoverable payment cannot permanently lock the rest of the parent bounty's funds.

### Proof of Concept
1. Fund a parent bounty and accept a curator.
2. Curator creates and funds a child bounty (`fund_child_bounty`), then awards it (`award_bounty`) to a beneficiary account that is guaranteed to fail the configured `Paymaster::pay` (e.g., an account with balance below ED for a non-sufficient asset kind, causing the underlying transfer to always error/never confirm).
3. `check_status` repeatedly returns `PaymentState::Failed`/`Pending`; `retry_payment` never succeeds because the beneficiary is structurally incapable of receiving the asset.
4. Attempt `close_bounty` on the parent bounty: it reverts with `Error::HasActiveChildBounty` forever, since `ChildBountiesPerParent` for the parent never reaches zero.
5. All remaining funds held by the parent bounty account (beyond the stuck child's share) are now permanently inaccessible, with no dispatchable in the pallet to force removal of the stuck child bounty.

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1099-1106)
```rust
			match child_bounty_id {
				None => {
					// Parent bounty can only be closed if it has no active child bounties.
					ensure!(
						ChildBountiesPerParent::<T, I>::get(parent_bounty_id) == 0,
						Error::<T, I>::HasActiveChildBounty
					);
					// Bounty can be closed by `RejectOrigin` or the curator.
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1214-1250)
```rust
				RefundAttempted { ref payment_status, ref curator } => {
					let new_payment_status = Self::do_check_refund_payment_status(
						parent_bounty_id,
						child_bounty_id,
						payment_status.clone(),
					)?;

					let new_status = match new_payment_status {
						PaymentState::Succeeded => {
							if let Some(curator) = curator {
								// Drop the curator deposit when payment succeeds
								// If the parent curator is also the child curator, there
								// is no deposit
								if let Some(curator_deposit) =
									CuratorDeposit::<T, I>::take(parent_bounty_id, child_bounty_id)
								{
									T::Consideration::drop(curator_deposit, curator)?;
								}
							}
							if let Some(_) = child_bounty_id {
								// Revert the value back to parent bounty
								ChildBountiesValuePerParent::<T, I>::mutate(
									parent_bounty_id,
									|total_value| *total_value = total_value.saturating_sub(value),
								);
							}
							// refund succeeded, cleanup the bounty
							Self::remove_bounty(parent_bounty_id, child_bounty_id, metadata);
							return Ok(Pays::No.into());
						},
						PaymentState::Pending |
						PaymentState::Failed |
						PaymentState::Attempted { .. } => BountyStatus::RefundAttempted {
							payment_status: new_payment_status,
							curator: curator.clone(),
						},
					};
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1338-1390)
```rust
			let (new_status, weight) = match status {
				FundingAttempted { ref payment_status, ref curator } => {
					let new_payment_status = Self::do_process_funding_payment(
						parent_bounty_id,
						child_bounty_id,
						asset_kind,
						value,
						Some(payment_status.clone()),
					)?;

					(
						FundingAttempted {
							payment_status: new_payment_status,
							curator: curator.clone(),
						},
						<T as Config<I>>::WeightInfo::retry_payment_funding(),
					)
				},
				RefundAttempted { ref curator, ref payment_status } => {
					let new_payment_status = Self::do_process_refund_payment(
						parent_bounty_id,
						child_bounty_id,
						asset_kind,
						value,
						Some(payment_status.clone()),
					)?;
					(
						RefundAttempted {
							curator: curator.clone(),
							payment_status: new_payment_status,
						},
						<T as Config<I>>::WeightInfo::retry_payment_refund(),
					)
				},
				PayoutAttempted { ref curator, ref beneficiary, ref payment_status } => {
					let new_payment_status = Self::do_process_payout_payment(
						parent_bounty_id,
						child_bounty_id,
						asset_kind,
						value,
						beneficiary.clone(),
						Some(payment_status.clone()),
					)?;
					(
						PayoutAttempted {
							curator: curator.clone(),
							beneficiary: beneficiary.clone(),
							payment_status: new_payment_status,
						},
						<T as Config<I>>::WeightInfo::retry_payment_payout(),
					)
				},
				_ => return Err(Error::<T, I>::UnexpectedStatus.into()),
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1687-1719)
```rust
	/// Initiates payment from the funding source to the child-/bounty account/location.
	fn do_process_funding_payment(
		parent_bounty_id: BountyIndex,
		child_bounty_id: Option<BountyIndex>,
		asset_kind: T::AssetKind,
		value: T::Balance,
		maybe_payment_status: Option<PaymentState<PaymentIdOf<T, I>>>,
	) -> Result<PaymentState<PaymentIdOf<T, I>>, DispatchError> {
		if let Some(payment_status) = maybe_payment_status {
			ensure!(payment_status.is_pending_or_failed(), Error::<T, I>::UnexpectedStatus);
		}

		let (source, beneficiary) = match child_bounty_id {
			None => (
				Self::funding_source_account(asset_kind.clone())?,
				Self::bounty_account(parent_bounty_id, asset_kind.clone())?,
			),
			Some(child_bounty_id) => (
				Self::bounty_account(parent_bounty_id, asset_kind.clone())?,
				Self::child_bounty_account(parent_bounty_id, child_bounty_id, asset_kind.clone())?,
			),
		};

		let id = <T as Config<I>>::Paymaster::pay(&source, &beneficiary, asset_kind, value)
			.map_err(|_| Error::<T, I>::FundingError)?;

		Self::deposit_event(Event::<T, I>::Paid {
			index: parent_bounty_id,
			child_index: child_bounty_id,
			payment_id: id,
		});

		Ok(PaymentState::Attempted { id })
```
