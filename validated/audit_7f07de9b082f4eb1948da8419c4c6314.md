Audit Report

## Title
Ignored `increase_balance` result in `fungible`/`fungibles` `Mutate::transfer` default implementation can silently burn transferred funds - (File: `substrate/frame/support/src/traits/tokens/fungible/regular.rs`, `substrate/frame/support/src/traits/tokens/fungibles/regular.rs`)

## Summary
The default `transfer` implementation shared by `fungible::Mutate` and `fungibles::Mutate` debits the source with `decrease_balance(...)?` and then discards the result of the destination-side `increase_balance(...)` with `let _ = ...`, before unconditionally emitting a `Transfer` event and returning `Ok(amount)`. Because `can_deposit` (a pre-check) and `increase_balance` (the actual mutation) are separate operations that can diverge in a concrete implementation, a deposit-time failure after debit is silently swallowed, producing a state where funds vanish from the source without appearing at the destination while a `Transfer` success event still fires.

## Finding Description
Both default implementations follow the identical vulnerable pattern: [1](#0-0) [2](#0-1) 

I confirmed a concrete, real divergence between the `can_deposit` pre-check and the actual `increase_balance` mutation in `pallet-assets`, which relies on this default `transfer` (it only overrides the `done_transfer` event hook, not `transfer` itself): [3](#0-2) 

`can_deposit` in `pallet-assets` calls `can_increase(asset, who, amount, provenance == Minted)`, and for a `transfer` the trait calls it with `Provenance::Extant`, so `increase_supply = false`: [4](#0-3) [5](#0-4) 

However, the actual `Unbalanced::increase_balance` used by the default `transfer` mutation calls the internal `Pallet::increase_balance` helper, which hardcodes `increase_supply = true` regardless of caller context: [6](#0-5) [7](#0-6) 

This means `can_deposit` (checked before the debit) skips the "does adding `amount` overflow total `supply`" check, while `increase_balance` (the actual write, invoked after the debit) performs that overflow check. If `details.supply.checked_add(&amount)` overflows (i.e., total asset supply near `T::Balance::MAX`), `can_increase`/`can_deposit` at the pre-check stage returns `Success` (since `increase_supply=false` suppresses the check), but the later `increase_balance` mutation returns `DepositConsequence::Overflow` → an `Err`. In the default `transfer`, `decrease_balance` on the source has already succeeded and removed the funds, and `let _ = Self::increase_balance(...)` throws this error away, followed by unconditional `done_transfer` (emitting `Transfer`) and `Ok(amount)`.

This exactly matches the reported pattern: an outer "should never fail" comment backed only by a discarded `Result`, not an atomic guarantee, and a demonstrated implementation (`pallet-assets`) whose `can_deposit` and `increase_balance` checks are not equivalent.

## Impact Explanation
If reachable, this breaks the "assets must conserve value and settle exactly once" invariant: the transferred amount is debited from the source, never credited to the destination, `total_issuance`/`supply` is desynchronized from the sum of account balances, and a `Transfer` event still fires misleading downstream observers. This is the correct impact category (fund loss due to a settlement bug) under the Polkadot SDK impact gate, if a reachable, unprivileged trigger path can be shown.

## Likelihood Explanation
I was able to confirm the underlying pattern (discarded `Result`) exists exactly as described in both `fungible` and `fungibles` `regular.rs`, and found one concrete divergence between `can_deposit` and `increase_balance` in `pallet-assets` (the `increase_supply` flag inconsistency). However, I could **not**, within the available tool budget, confirm that:
1. This trait-level default `Mutate::transfer` (as opposed to `pallet-assets`'s own extrinsic-level `do_transfer` in `functions.rs`, which is a separate code path used by the pallet's public dispatchables) is actually invoked on a live, unprivileged-attacker-reachable path (e.g., an XCM `fungibles`-based asset transactor or another pallet's genuinely public entry point) in this repository.
2. The overflow-based divergence is practically triggerable — it requires an asset's total `supply` to be within `amount` of `T::Balance::MAX`, an extreme and generally impractical precondition for governance-created assets, and I did not verify whether any asset configuration in-repo could realistically reach that state.
3. Any other concrete `Unbalanced` implementor (e.g. `assets-holder`, `assets-freezer`, XCM adapters) has a similarly reachable divergence — I confirmed `assets-holder` merely proxies to `pallet-assets`, inheriting the same characteristics, but did not exhaustively check others.

Given the required checks (reachable exploit path from attacker input, exact wrong-value naming, reproducible test) are not fully satisfiable with the evidence gathered — the divergence found is a genuine but extreme edge case (near-`Balance::MAX` supply overflow) and I could not confirm an unprivileged, public-extrinsic-reachable call path into this specific default trait method for `pallet-assets` (its own extrinsics use `do_transfer` instead) — I cannot certify this as a live, exploitable vulnerability with the current evidence, but the underlying anti-pattern (silently discarding a value-moving `Result`) is real and present in shared library code as described.

## Recommendation
Do not discard the result of `increase_balance` in the default `transfer` implementations for both `fungible::Mutate` and `fungibles::Mutate`. Wrap the debit/credit pair in a transactional context (`with_transaction`) so a deposit failure rolls back the prior withdrawal, or propagate the error with `?` and require callers/implementors to guarantee `can_deposit` and `increase_balance` use identical logic (e.g., fix the `increase_supply` parameter inconsistency in `pallet-assets`'s `can_increase`/`increase_balance` call sites so pre-check and mutation agree).

## Proof of Concept
Not independently verified as reproducible against a public/unprivileged entry point within the available investigation budget; conceptual PoC:
1. Configure a `pallet-assets` asset whose `supply` is within `amount` of `T::Balance::MAX`.
2. Trigger a call path that uses `<pallet_assets::Pallet<T,I> as fungibles::Mutate<AccountId>>::transfer(asset, source, dest, amount, preservation)` (default trait method) rather than the pallet's own `do_transfer`.
3. Observe `can_deposit`(`Extant`) pass (overflow check skipped due to `increase_supply=false`), `decrease_balance` on `source` succeed, `increase_balance` on `dest` fail with `Overflow` and be discarded, `Transfer` event fire, and `Ok(amount)` returned while `dest`'s balance is unchanged and `source`'s balance is reduced.
4. This step requires confirming an actual unprivileged caller of the generic trait method in a production runtime, which was not established in this review.

### Citations

**File:** substrate/frame/support/src/traits/tokens/fungible/regular.rs (L321-339)
```rust
	fn transfer(
		source: &AccountId,
		dest: &AccountId,
		amount: Self::Balance,
		preservation: Preservation,
	) -> Result<Self::Balance, DispatchError> {
		let _extra = Self::can_withdraw(source, amount).into_result(preservation != Expendable)?;
		Self::can_deposit(dest, amount, Extant).into_result()?;
		if source == dest {
			return Ok(amount);
		}

		Self::decrease_balance(source, amount, BestEffort, preservation, Polite)?;
		// This should never fail as we checked `can_deposit` earlier. But we do a best-effort
		// anyway.
		let _ = Self::increase_balance(dest, amount, BestEffort);
		Self::done_transfer(source, dest, amount);
		Ok(amount)
	}
```

**File:** substrate/frame/support/src/traits/tokens/fungibles/regular.rs (L366-386)
```rust
	fn transfer(
		asset: Self::AssetId,
		source: &AccountId,
		dest: &AccountId,
		amount: Self::Balance,
		preservation: Preservation,
	) -> Result<Self::Balance, DispatchError> {
		let _extra = Self::can_withdraw(asset.clone(), source, amount)
			.into_result(preservation != Expendable)?;
		Self::can_deposit(asset.clone(), dest, amount, Extant).into_result()?;
		if source == dest {
			return Ok(amount);
		}

		Self::decrease_balance(asset.clone(), source, amount, BestEffort, preservation, Polite)?;
		// This should never fail as we checked `can_deposit` earlier. But we do a best-effort
		// anyway.
		let _ = Self::increase_balance(asset.clone(), dest, amount, BestEffort);
		Self::done_transfer(asset, source, dest, amount);
		Ok(amount)
	}
```

**File:** substrate/frame/assets/src/impl_fungibles.rs (L64-71)
```rust
	fn can_deposit(
		asset: Self::AssetId,
		who: &<T as SystemConfig>::AccountId,
		amount: Self::Balance,
		provenance: Provenance,
	) -> DepositConsequence {
		Pallet::<T, I>::can_increase(asset, who, amount, provenance == Minted)
	}
```

**File:** substrate/frame/assets/src/impl_fungibles.rs (L103-115)
```rust
	fn done_transfer(
		asset_id: Self::AssetId,
		source: &<T as SystemConfig>::AccountId,
		dest: &<T as SystemConfig>::AccountId,
		amount: Self::Balance,
	) {
		Self::deposit_event(Event::Transferred {
			asset_id,
			from: source.clone(),
			to: dest.clone(),
			amount,
		});
	}
```

**File:** substrate/frame/assets/src/impl_fungibles.rs (L209-217)
```rust
	fn increase_balance(
		asset: T::AssetId,
		who: &T::AccountId,
		amount: Self::Balance,
		_: Precision,
	) -> Result<Self::Balance, DispatchError> {
		Self::increase_balance(asset, who, amount, |_| Ok(()))?;
		Ok(amount)
	}
```

**File:** substrate/frame/assets/src/functions.rs (L137-152)
```rust
	pub(super) fn can_increase(
		id: T::AssetId,
		who: &T::AccountId,
		amount: T::Balance,
		increase_supply: bool,
	) -> DepositConsequence {
		let details = match Asset::<T, I>::get(&id) {
			Some(details) => details,
			None => return DepositConsequence::UnknownAsset,
		};
		if details.status == AssetStatus::Destroying {
			return DepositConsequence::UnknownAsset;
		}
		if increase_supply && details.supply.checked_add(&amount).is_none() {
			return DepositConsequence::Overflow;
		}
```

**File:** substrate/frame/assets/src/functions.rs (L485-497)
```rust
	pub(super) fn increase_balance(
		id: T::AssetId,
		beneficiary: &T::AccountId,
		amount: T::Balance,
		check: impl FnOnce(
			&mut AssetDetails<T::Balance, T::AccountId, DepositBalanceOf<T, I>>,
		) -> DispatchResult,
	) -> DispatchResult {
		if amount.is_zero() {
			return Ok(());
		}

		Self::can_increase(id.clone(), beneficiary, amount, true).into_result()?;
```
