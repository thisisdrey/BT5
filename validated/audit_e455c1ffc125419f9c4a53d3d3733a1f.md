### Title
Silent transfer-failure in `claim_bounty`/`claim_child_bounty` lets the permissionless `reclaim_bounty_funds` sweep a beneficiary's unpaid payout into the Treasury - (File: `substrate/frame/bounties/src/lib.rs`)

### Summary
`pallet-bounties::claim_bounty` (and the analogous `pallet-child-bounties::claim_child_bounty`) removes the bounty's storage record unconditionally, even when the `T::Currency::transfer` calls that pay the beneficiary and curator fail, because the results are only checked with `debug_assert!`, which is compiled out in production (release) builds. Once the storage entry is gone, the new permissionless `reclaim_bounty_funds` extrinsic (added in [1](#0-0) ) treats the absence of the `Bounties` entry as proof that the bounty account holds only "stranded" dust, and sweeps its entire remaining balance to the Treasury. This mirrors the reported GLX bug: a sweep/withdraw entrypoint (`withdrawNative`) has no way to distinguish collected fees from funds still owed to a user, so it transfers user-owed assets to the wrong beneficiary (the protocol treasury instead of the rightful claimant).

### Finding Description
In `claim_bounty`, the payout logic is: [2](#0-1) 

The transfers to `curator` and `beneficiary` are checked only via `debug_assert!(res.is_ok())`, and immediately afterward `*maybe_bounty = None;` unconditionally clears the storage entry regardless of whether the transfers actually succeeded. `debug_assert!` compiles to a no-op in non-debug builds (the builds used for production runtimes), so any transfer failure is silently swallowed and the funds remain in `bounty_account` while the bounty record is deleted.

A `Currency::transfer` with `AllowDeath` can still fail with `TokenError::BelowMinimum` when the **destination** account does not exist and the transferred amount is below `ExistentialDeposit` — `AllowDeath` only relaxes the requirement on the *source* account, not the receiving account's creation minimum. Any beneficiary or curator address that has never been funded and receives a payout/fee below the existential deposit will trigger this failure path.

The new `reclaim_bounty_funds` extrinsic uses exactly the post-condition created by this bug as its authorization check: [3](#0-2) 

It only verifies `!Bounties::<T, I>::contains_key(bounty_id)` — it cannot distinguish between "bounty fully and correctly paid out, dust remains" and "bounty payout silently failed, full payout remains stuck." It is `ensure_signed`-only (permissionless) and sweeps the entire `TransferAllAssets` balance of `bounty_account` to `Self::account_id()` (the Treasury), as shown in `TransferFungible::force_transfer_all_assets`: [4](#0-3) 

The same debug_assert-guarded transfer pattern exists in `claim_child_bounty`: [5](#0-4) 

### Impact Explanation
The corrupted value is the `Bounties<T, I>` (or `ChildBounties<T>`) storage entry: it is set to `None` even though the funds it was gating (the beneficiary's payout and/or curator's fee, held in `bounty_account`) were never actually transferred out. This breaks the invariant that "bounty storage removed ⇒ bounty account holds no more claimant-owed funds," which `reclaim_bounty_funds` relies on as its sole safety check. Any unprivileged account can then call `reclaim_bounty_funds` to permanently redirect the trapped beneficiary/curator payout to the Treasury. This is an irreversible, unauthorized redirection of user-owed funds to the wrong beneficiary — the exact bug class described in the report (loss of segregation between collected/treasury funds and user-owed assets).

### Likelihood Explanation
This requires no privileged actor, governance, relayer, or malicious validator — only an unprivileged signed account to (a) become a bounty beneficiary/curator with a small enough payout while unfunded (which a legitimate proposer/curator setup can trivially create, or which an attacker can arrange for themselves as the beneficiary of their own proposed bounty), and (b) call the permissionless `reclaim_bounty_funds`. Because `debug_assert!` is a standard, deliberate no-op in release builds used by production Substrate/Polkadot-SDK runtimes, this is a live-scope condition, not a testing artifact.

### Recommendation
Replace the `debug_assert!(res.is_ok())` checks in `claim_bounty` and `claim_child_bounty` with real error propagation (e.g. `res?` or explicit fallback handling) so that a failed transfer aborts the storage removal, or falls back to `Preservation::Expendable`/keeps a residual claim path for the beneficiary. Alternatively, keep the bounty record (or a "pending claim" marker) until both transfers are confirmed successful, so `reclaim_bounty_funds` can never sweep funds still owed to a beneficiary/curator.

### Proof of Concept
1. Propose and approve a bounty with `value = V`; assign and accept a curator with `fee = F` such that `payout = V - F` is smaller than `ExistentialDeposit`.
2. Curator calls `award_bounty(bounty_id, beneficiary)` where `beneficiary` is a fresh account with zero balance (never funded, no ED).
3. After the payout delay, call `claim_bounty(bounty_id)` on a release build. `T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath)` returns `Err(TokenError::BelowMinimum)` because the destination account cannot be created below ED; the `debug_assert!` is compiled out, so this error is ignored and execution continues.
4. `*maybe_bounty = None` still executes, removing the `Bounties` entry, even though `payout` remains in `bounty_account`.
5. Any account calls `reclaim_bounty_funds(bounty_id)`; the `!Bounties::contains_key` check passes, and the trapped `payout` (and any similarly-stuck curator fee) is transferred to the Treasury account instead of the intended beneficiary/curator — an irreversible loss of the beneficiary's bounty reward.

### Citations

**File:** prdoc/pr_11045.prdoc (L1-14)
```text
title: '[pallet-bounties]: add `reclaim_bounty_funds` to reclaim stranded funds from
  closed bounty accounts'
doc:
- audience: Runtime Dev
  description: |-
    fixes https://github.com/paritytech/polkadot-sdk/issues/10996

    This PR adds a permissionless `reclaim_bounty_funds` extrinsic that moves all
    funds stranded in a closed bounty's account back to the treasury in a single
    call. It reclaims both the native token and any fungible assets configured via
    the `TransferAllAssets` associated type. Native funds are moved using
    `transfer_all` semantics (reducible balance with `Expendable` preservation) so
    locks and freezes are respected. The call is free on success and paid on a no-op,
    so it cannot be used to grief the network.
```

**File:** substrate/frame/bounties/src/lib.rs (L238-257)
```rust
/// Transfer the entire balance of a single `fungible::Mutate` currency from one account to
/// another.
///
/// Suitable for runtimes that expose exactly one relevant currency (e.g. native-only runtimes
/// without multi-asset support). For runtimes with multi-asset support, prefer
/// [`TransferAllFungibles`] with all relevant asset IDs in `RelevantAssets`.
pub struct TransferFungible<AccountId, Currency>(core::marker::PhantomData<(AccountId, Currency)>);
impl<AccountId, C> TransferAllAssets<AccountId> for TransferFungible<AccountId, C>
where
	C: FungibleMutate<AccountId>,
	AccountId: Eq,
{
	fn force_transfer_all_assets(from: &AccountId, to: &AccountId) -> Result<bool, DispatchError> {
		let balance = C::reducible_balance(from, Preservation::Expendable, Fortitude::Polite);
		if balance.is_zero() {
			return Ok(false);
		}
		C::transfer(from, to, balance, Preservation::Expendable)?;
		Ok(true)
	}
```

**File:** substrate/frame/bounties/src/lib.rs (L808-830)
```rust
					let bounty_account = Self::bounty_account_id(bounty_id);
					let balance = T::Currency::free_balance(&bounty_account);
					let fee = bounty.fee.min(balance); // just to be safe
					let payout = balance.saturating_sub(fee);
					let err_amount = T::Currency::unreserve(&curator, bounty.curator_deposit);
					debug_assert!(err_amount.is_zero());

					// Get total child bounties curator fees, and subtract it from the parent
					// curator fee (the fee in present referenced bounty, `self`).
					let children_fee = T::ChildBountyManager::children_curator_fees(bounty_id);
					debug_assert!(children_fee <= fee);

					let final_fee = fee.saturating_sub(children_fee);
					let res =
						T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
					debug_assert!(res.is_ok());
					let res =
						T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
					debug_assert!(res.is_ok());

					*maybe_bounty = None;

					BountyDescriptions::<T, I>::remove(bounty_id);
```

**File:** substrate/frame/bounties/src/lib.rs (L1058-1090)
```rust
		#[pallet::call_index(11)]
		#[pallet::weight(<T as Config<I>>::WeightInfo::reclaim_bounty_funds())]
		pub fn reclaim_bounty_funds(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			// A live bounty still manages its account, so leave it untouched.
			ensure!(!Bounties::<T, I>::contains_key(bounty_id), Error::<T, I>::BountyStillActive);

			debug_assert!(
				T::ChildBountyManager::child_bounties_count(bounty_id) == 0,
				"child bounties should not exist for a closed bounty"
			);

			let bounty_account = Self::bounty_account_id(bounty_id);
			let treasury_account = Self::account_id();

			let transferred = T::TransferAllAssets::force_transfer_all_assets(
				&bounty_account,
				&treasury_account,
			)?;

			// Free only if something moved, otherwise paid to prevent griefing.
			if !transferred {
				return Ok(Pays::Yes.into());
			}

			Self::deposit_event(Event::<T, I>::BountyFundsReclaimed { bounty_id });

			Ok(Pays::No.into())
		}
```

**File:** substrate/frame/child-bounties/src/lib.rs (L714-744)
```rust
						// Make curator fee payment.
						let child_bounty_account =
							Self::child_bounty_account_id(parent_bounty_id, child_bounty_id);
						let balance = T::Currency::free_balance(&child_bounty_account);
						let curator_fee = child_bounty.fee.min(balance);
						let payout = balance.saturating_sub(curator_fee);

						// Unreserve the curator deposit. Should not fail
						// because the deposit is always reserved when curator is
						// assigned.
						let _ = T::Currency::unreserve(curator, child_bounty.curator_deposit);

						// Make payout to child-bounty curator.
						// Should not fail because curator fee is always less than bounty value.
						let fee_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							curator,
							curator_fee,
							AllowDeath,
						);
						debug_assert!(fee_transfer_result.is_ok());

						// Make payout to beneficiary.
						// Should not fail.
						let payout_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							beneficiary,
							payout,
							AllowDeath,
						);
						debug_assert!(payout_transfer_result.is_ok());
```
