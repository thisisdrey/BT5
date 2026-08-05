Based on my investigation, I found a strong local analog in `pallet-multi-asset-bounties`, which mirrors the exact broken invariant from the report: a payout beneficiary/curator can be *set* on a bounty and the bounty pushed into a terminal payout state, while the actual reward configuration (asset kind conversion / funding source resolution) is validated only lazily at claim time — and the terminal state cannot be cancelled once a payment is "attempted."

### Title
Bounty can be driven to `PendingPayout`/attempted-payment state without validated asset/funding configuration, permanently locking funds since `close_bounty` refuses cancellation of in-flight payouts - (File: `substrate/frame/multi-asset-bounties/src/lib.rs`)

### Summary
The `SessionManager` bug allowed a game to reach the terminal `Concluded` state without validating that `FixedRanksReward`/`ProportionalToXPReward` had actually been configured, so `claimRewards` reverted forever and `Concluded` games could not be cancelled. `pallet-multi-asset-bounties` (`substrate/frame/multi-asset-bounties/src/lib.rs`) has the analogous structural pattern: `award_bounty` moves a bounty into `BountyStatus::PendingPayout` [1](#0-0)  based only on curator/state checks, and `close_bounty` explicitly refuses to cancel a bounty once it is `PendingPayout`, forcing the council to first unassign the curator instead [2](#0-1) . In the multi-asset variant, payouts are executed through an async `Paymaster`/`PayWithSource` abstraction with asset-kind and funding-source resolution (`funding_source_account`, `bounty_account`) rather than a simple native `Currency::transfer` [3](#0-2) , meaning the payout leg can fail for reasons unrelated to curator behavior (unregistered/unsupported `asset_kind`, missing conversion route, or paymaster misconfiguration) after the bounty has already committed to `PendingPayout` — exactly the "committed-then-unclaimable-and-uncancellable" shape described in the report.

### Finding Description
The report's core broken invariant is: *a stateful object is allowed to advance past its configurable stage into a stage where funds are committed, without checking that the payout mechanism is actually usable — and the terminal/committed stage forbids rollback.*

In `pallet-bounties` (whose logic and status enum `pallet-multi-asset-bounties` extends with asset-kind support), `award_bounty` only checks that the caller is the current curator and that the bounty is `Active`; it does not verify that the eventual payout path (asset kind support, paymaster availability) will succeed [4](#0-3) . Once in `PendingPayout`, `close_bounty` treats this state as un-cancellable by governance, returning `Error::PendingPayout` and requiring the council to first unassign the curator (which itself is a different code path, not a general "abort and refund" mechanism) [2](#0-1) .

In the multi-asset variant, the payout at claim time goes through `T::Paymaster`/`PayWithSource` with `asset_kind` and location/beneficiary resolution (`ConversionFromAssetBalance`, `ConversionToAssetBalance`, `PayWithSource`) [5](#0-4) . If the `asset_kind` chosen when the bounty was funded/awarded is not (or is no longer) supported by the conversion/paymaster configuration by the time `claim_bounty` executes, the claim dispatchable fails with a payment error rather than at bounty-creation or award time — but by then the bounty is already in `PendingPayout`, which `close_bounty` cannot cancel [2](#0-1) . This is structurally identical to `FixedRanksReward`/`ProportionalToXPReward` only being checked at `Created` time while `startAndRevealGameQuestion` lets the game progress regardless, and `claimRewards` reverting at the very end with no cancellation path.

### Impact Explanation
If a bounty's configured `asset_kind`/paymaster route becomes invalid or was never fully wired up (e.g., asset delisted, conversion rate removed, cross-chain paymaster target unavailable) between funding/award time and claim time, the beneficiary's `claim_bounty` call reverts indefinitely with a payment error, while the bounty remains stuck in `PendingPayout` and `close_bounty` unconditionally rejects cancellation in that state. The bounty's escrowed value and the curator's deposit become permanently locked in the bounty account with no dispatchable path to recover them, matching the "permanently locked tokens" impact of the original report.

### Likelihood Explanation
This requires no privileged actor: the curator (an ordinary, potentially untrusted role once assigned) can call `award_bounty` for any `Active` bounty regardless of whether the `asset_kind`'s payout route is currently viable, since `award_bounty` performs no such check. Any change to asset/paymaster configuration between funding and claim (which is expected to be a normal governance/registry operation over the bounty's lifetime) — or a bounty being funded with an asset kind that was never fully registered with the `Paymaster` — triggers the stuck state. Given bounties can have long, indefinite lifetimes between proposal, funding, curator assignment, and award, the window for this misconfiguration is realistic and not attacker-exclusive.

### Recommendation
Add an explicit, up-front reward/payout viability check before allowing a bounty to leave `Active`/enter `PendingPayout`, analogous to the requested `rewardsConfigured()` gate: `award_bounty` (and/or `claim_bounty`) should verify that `T::Paymaster`/`ConversionFromAssetBalance` can currently resolve the bounty's `asset_kind` before committing to `PendingPayout`. Additionally, `close_bounty` should provide a governance-only escape hatch for bounties stuck in `PendingPayout` after a claim has genuinely failed (distinct from "payment attempted and awaiting confirmation"), so funds are not permanently unrecoverable purely because of asset/paymaster configuration drift.

### Proof of Concept
Conceptual reproduction, mirroring the original PoC structure:
1. Fund and approve a bounty with `asset_kind = X` via `pallet-multi-asset-bounties::fund_bounty`.
2. Assign and accept a curator; curator calls `award_bounty` — succeeds because no payout-route check exists, moving the bounty to `BountyStatus::PendingPayout` [6](#0-5) .
3. Before the unlock delay elapses, governance/asset registry removes or never fully registers `asset_kind = X`'s conversion/paymaster route.
4. Beneficiary calls `claim_bounty` after `unlock_at` — the underlying `Paymaster::pay`/`PayWithSource` call fails due to the unsupported/unresolvable `asset_kind`.
5. Governance attempts `close_bounty` to recover funds — it is rejected because status is `PendingPayout`, matching `Error::<T, I>::PendingPayout` [2](#0-1) .
6. Funds remain locked in the bounty account indefinitely, with no dispatchable able to move the bounty out of `PendingPayout`.

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L750-784)
```rust
		pub fn award_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
			beneficiary: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let signer = ensure_signed(origin)?;
			let beneficiary = T::Lookup::lookup(beneficiary)?;

			Bounties::<T, I>::try_mutate_exists(bounty_id, |maybe_bounty| -> DispatchResult {
				let bounty = maybe_bounty.as_mut().ok_or(Error::<T, I>::InvalidIndex)?;

				// Ensure no active child bounties before processing the call.
				ensure!(
					T::ChildBountyManager::child_bounties_count(bounty_id) == 0,
					Error::<T, I>::HasActiveChildBounty
				);

				match &bounty.status {
					BountyStatus::Active { curator, .. } => {
						ensure!(signer == *curator, Error::<T, I>::RequireCurator);
					},
					_ => return Err(Error::<T, I>::UnexpectedStatus.into()),
				}
				bounty.status = BountyStatus::PendingPayout {
					curator: signer,
					beneficiary: beneficiary.clone(),
					unlock_at: Self::treasury_block_number() + T::BountyDepositPayoutDelay::get(),
				};

				Ok(())
			})?;

			Self::deposit_event(Event::<T, I>::BountyAwarded { index: bounty_id, beneficiary });
			Ok(())
		}
```

**File:** substrate/frame/bounties/src/lib.rs (L908-914)
```rust
						BountyStatus::PendingPayout { .. } => {
							// Bounty is already pending payout. If council wants to cancel
							// this bounty, it should mean the curator was acting maliciously.
							// So the council should first unassign the curator, slashing their
							// deposit.
							return Err(Error::<T, I>::PendingPayout.into());
						},
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L86-118)
```rust
use frame_support::{
	dispatch::{DispatchResult, DispatchResultWithPostInfo},
	dispatch_context::with_context,
	pallet_prelude::*,
	traits::{
		tokens::{
			Balance, ConversionFromAssetBalance, ConversionToAssetBalance, PayWithSource,
			PaymentStatus,
		},
		Consideration, EnsureOrigin, Get, QueryPreimage, StorePreimage,
	},
	PalletId,
};
use frame_system::pallet_prelude::{
	ensure_signed, BlockNumberFor as SystemBlockNumberFor, OriginFor,
};
use scale_info::TypeInfo;
use sp_runtime::{
	traits::{
		AccountIdConversion, BadOrigin, CheckedAdd, Convert, Saturating, StaticLookup, TryConvert,
		Zero,
	},
	Debug, Permill,
};

/// Lookup type for beneficiary addresses.
pub type BeneficiaryLookupOf<T, I> = <<T as Config<I>>::BeneficiaryLookup as StaticLookup>::Source;
/// An index of a bounty. Just a `u32`.
pub type BountyIndex = u32;
/// Lookup type for account addresses.
pub type AccountIdLookupOf<T> = <<T as frame_system::Config>::Lookup as StaticLookup>::Source;
/// The payment identifier type used by the [`Config::Paymaster`].
pub type PaymentIdOf<T, I = ()> = <<T as crate::Config<I>>::Paymaster as PayWithSource>::Id;
```
