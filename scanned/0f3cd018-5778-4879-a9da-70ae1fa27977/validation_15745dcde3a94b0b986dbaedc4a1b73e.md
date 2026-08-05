### Title
Permissionless `touch_other` lets an attacker force an unwanted, undestroyable asset account onto a victim, consuming their consumer-reference budget and blocking legitimate account operations - (File: `substrate/frame/assets/src/functions.rs`, `substrate/frame/assets/src/lib.rs`)

### Summary
The Morpho report's core broken invariant is: an unprivileged attacker can push an asset the victim never asked for into the victim's account state, and that state cannot be removed by the victim, causing side effects that block core account operations (withdraw, borrow, liquidate). The `pallet-assets` `touch_other` extrinsic reproduces the same primitive: any signed account can create an `Account` entry for an *arbitrary* `who` for a non-sufficient asset, paying the deposit itself, without the victim's consent [1](#0-0) . Because this consumes one of the victim's limited `frame_system` consumer references via `new_account`/`inc_consumers` [2](#0-1) , and the deposit/account can only be removed by the depositor or the asset admin (not the victim alone, see `do_refund_other`'s caller check) [3](#0-2) , a griefer can repeatedly `touch_other` the victim across many distinct non-sufficient asset ids to approach `MaxConsumers`, degrading or blocking the victim's ability to perform other consumer-requiring operations (e.g. staking bond, other asset/NFT account creation) — the same "poisoned, unremovable state blocks victim operations" pattern as the Aave/Morpho report.

### Finding Description
`Pallet::touch_other` is a fully permissionless call: `origin` can be any signed account, and `who` is an arbitrary target account chosen by the caller [4](#0-3) . It calls `do_touch(id, who, origin)`, which creates a zero-balance `Account` entry for `who` for asset `id`, with the deposit reserved from the caller (`depositor`), not from `who` [5](#0-4) .

Inside `new_account`, if the asset is *not* marked `is_sufficient`, the victim's `frame_system` consumer counter is incremented (`inc_consumers`), subject only to the global `MaxConsumers` cap, not to victim consent [6](#0-5) . The `should_touch` check used by other pallets (e.g. asset-conversion) only guards *their own* calls into `touch`; it does not prevent a direct attacker call to `touch_other` on any account.

Removing this state again — `do_refund_other` — requires `caller == depositor || caller == details.admin`; the victim account itself has no path to remove a `DepositFrom(attacker, deposit)` reason it did not create [7](#0-6) . Only the original depositor (the attacker, who has no incentive to undo the griefing) or the asset admin can free the consumer slot. This mirrors the Aave situation exactly: the victim cannot unilaterally remove the unwanted state that a third party attached to their account, and that state has systemic side effects (consumer exhaustion) beyond the immediate asset itself.

By repeating this against a victim across `MaxConsumers` distinct non-sufficient asset ids (which any attacker can `force`-independently create if permissionless asset creation is enabled, or simply reuse existing live non-sufficient assets on the chain), the attacker exhausts the victim's consumer budget. `can_inc_consumer`/`can_accrue_consumers` checks in other pallets (staking bonding, further asset `touch`, NFT `set_accept_ownership`, etc.) will then start failing for the victim with `TooManyConsumers`, exactly analogous to Morpho's victim being unable to withdraw/borrow/liquidate because of unremovable unsolicited state.

### Impact Explanation
This degrades or blocks legitimate account operations for a targeted victim without needing any privileged, admin, governance, validator, or peer role — a pure unprivileged-attacker griefing path against public dispatch (`touch_other`). It matches the "public underpriced work that degrades... stalls processing" and "unauthorized... state" categories in the impact gate: an attacker can, at low/no net cost to themselves (recoverable if they later call `refund_other` — but nothing forces them to), permanently or semi-permanently occupy a victim's finite consumer-reference budget, a systemic per-account resource shared across all pallets requiring `inc_consumers` (staking, other asset pallets, NFTs, etc.).

### Likelihood Explanation
`touch_other` is a stable, documented, permissionless extrinsic with call index 29 [4](#0-3) ; no governance or privileged role is required. The only cost to the attacker is the `AssetAccountDeposit` per touch, which is reserved from the attacker, not burned, and can be reclaimed later via `refund_other` if the attacker wishes — but the attacker controls the timing, meaning the griefing window is fully attacker-controlled. The victim has no self-service remedy. Likelihood is bounded mainly by needing `MaxConsumers` (a per-runtime constant) distinct qualifying assets and the deposit capital, both readily satisfiable on chains with several live non-sufficient assets.

### Recommendation
- Require the target account (`who`) to opt in (e.g. via a pre-signed approval or a "consent" flag) before `touch_other` can consume one of its consumer references, or
- Allow the victim account itself to unilaterally call `refund_other`-style cleanup on `DepositFrom` entries regardless of who the depositor is (rather than restricting removal to `depositor == caller || caller == admin`), and/or
- Reserve a portion of `MaxConsumers` exclusively for self-initiated (victim-consented) consumer references, analogous to the existing `can_accrue_consumers(who, 2)` buffer used for ordinary `transfer`-triggered account creation, so that third-party `touch_other` calls cannot exhaust the full budget.

### Proof of Concept
1. Chain has `N = MaxConsumers` (e.g. 16) live, non-sufficient assets `A_1..A_N` already listed (or attacker force-creates/creates them if permissionless creation is allowed and funds it).
2. Attacker (any signed account with enough balance to cover `N` deposits of `AssetAccountDeposit`) calls, for each `i` in `1..N`:
   `Assets::touch_other(attacker_origin, A_i, victim)`
   — succeeds each time per `do_touch` (`substrate/frame/assets/src/functions.rs:342-367`), incrementing the victim's `frame_system` consumer counter each time (since these assets are non-sufficient) via `new_account` (`substrate/frame/assets/src/functions.rs:85-93`).
3. After `N` calls, `System::consumers(victim) == MaxConsumers`.
4. Victim now attempts any operation requiring a fresh consumer reference on their account — e.g. `Staking::bond`, or `Assets::touch`/`transfer` creating a new non-sufficient asset account, or `Uniques::set_accept_ownership` — and it fails with `DispatchError::TooManyConsumers`, mirroring the "victim cannot withdraw/borrow/liquidate" outcome from the Morpho report.
5. Victim cannot self-remove any of the `A_i` accounts via `refund_other`, because `do_refund_other`'s caller check (`substrate/frame/assets/src/functions.rs:429-431`) requires the caller to be the original depositor (the attacker) or the asset admin — not the victim.

### Citations

**File:** substrate/frame/assets/src/lib.rs (L1808-1830)
```rust
		/// Create an asset account for `who`.
		///
		/// A deposit will be taken from the signer account.
		///
		/// - `origin`: Must be Signed; the signer account must have sufficient funds for a deposit
		///   to be taken.
		/// - `id`: The identifier of the asset for the account to be created, the asset status must
		///   be live.
		/// - `who`: The account to be created.
		///
		/// Emits `Touched` event when successful.
		#[pallet::call_index(29)]
		#[pallet::weight(T::WeightInfo::touch_other())]
		pub fn touch_other(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			who: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let who = T::Lookup::lookup(who)?;
			let id: T::AssetId = id.into();
			Self::do_touch(id, who, origin)
		}
```

**File:** substrate/frame/assets/src/functions.rs (L68-97)
```rust
	pub(super) fn new_account(
		who: &T::AccountId,
		d: &mut AssetDetails<T::Balance, T::AccountId, DepositBalanceOf<T, I>>,
		maybe_deposit: Option<(&T::AccountId, DepositBalanceOf<T, I>)>,
	) -> Result<ExistenceReasonOf<T, I>, DispatchError> {
		let accounts = d.accounts.checked_add(1).ok_or(ArithmeticError::Overflow)?;
		let reason = if let Some((depositor, deposit)) = maybe_deposit {
			if depositor == who {
				ExistenceReason::DepositHeld(deposit)
			} else {
				ExistenceReason::DepositFrom(depositor.clone(), deposit)
			}
		} else if d.is_sufficient {
			frame_system::Pallet::<T>::inc_sufficients(who);
			d.sufficients.saturating_inc();
			ExistenceReason::Sufficient
		} else {
			frame_system::Pallet::<T>::inc_consumers(who)
				.map_err(|_| Error::<T, I>::UnavailableConsumer)?;
			// We ensure that we can still increment consumers once more because we could otherwise
			// allow accidental usage of all consumer references which could cause grief.
			if !frame_system::Pallet::<T>::can_inc_consumer(who) {
				frame_system::Pallet::<T>::dec_consumers(who);
				return Err(Error::<T, I>::UnavailableConsumer.into());
			}
			ExistenceReason::Consumer
		};
		d.accounts = accounts;
		Ok(reason)
	}
```

**File:** substrate/frame/assets/src/functions.rs (L342-367)
```rust
	/// Creates an account for `who` to hold asset `id` with a zero balance and takes a deposit.
	pub(super) fn do_touch(
		id: T::AssetId,
		who: T::AccountId,
		depositor: T::AccountId,
	) -> DispatchResult {
		ensure!(!Account::<T, I>::contains_key(&id, &who), Error::<T, I>::AlreadyExists);
		let deposit = T::AssetAccountDeposit::get();
		let mut details = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
		ensure!(details.status == AssetStatus::Live, Error::<T, I>::AssetNotLive);
		let reason = Self::new_account(&who, &mut details, Some((&depositor, deposit)))?;
		T::Currency::reserve(&depositor, deposit)?;
		Asset::<T, I>::insert(&id, details);
		Account::<T, I>::insert(
			&id,
			&who,
			AssetAccountOf::<T, I> {
				balance: Zero::zero(),
				status: AccountStatus::Liquid,
				reason,
				extra: T::Extra::default(),
			},
		);
		Self::deposit_event(Event::Touched { asset_id: id, who, depositor });
		Ok(())
	}
```

**File:** substrate/frame/assets/src/functions.rs (L418-433)
```rust
	pub(super) fn do_refund_other(
		id: T::AssetId,
		who: &T::AccountId,
		maybe_check_caller: Option<T::AccountId>,
	) -> DispatchResult {
		let mut account = Account::<T, I>::get(&id, &who).ok_or(Error::<T, I>::NoDeposit)?;
		let (depositor, deposit) =
			account.reason.take_deposit_from().ok_or(Error::<T, I>::NoDeposit)?;
		let mut details = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
		ensure!(details.status == AssetStatus::Live, Error::<T, I>::AssetNotLive);
		ensure!(!account.status.is_frozen(), Error::<T, I>::Frozen);
		if let Some(caller) = maybe_check_caller {
			ensure!(caller == depositor || caller == details.admin, Error::<T, I>::NoPermission);
		}
		ensure!(account.balance.is_zero(), Error::<T, I>::WouldBurn);
		Self::ensure_account_can_die(id.clone(), who)?;
```
