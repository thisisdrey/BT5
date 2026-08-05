### Title
`pallet-psm::mint` mints internal stablecoin against an external-asset transfer whose actual moved amount is never verified - ([File: substrate/frame/psm/src/lib.rs])

### Summary
`pallet-psm` (`substrate/frame/psm/src/lib.rs`) is a local Peg-Stability-Module pallet that is structurally identical to the reported `Trading.sol`/`StableVault` pattern: a user deposits an *external* asset, the pallet "checks" the transfer, and then unconditionally mints a new *internal* stablecoin to the caller. Just like the external report — where an unchecked `transferFrom` return value let the contract mint `StableVault` tokens without a real deposit — the generic `fungibles::Mutate::transfer` default implementation that `T::Fungibles::transfer` resolves to (`substrate/frame/support/src/traits/tokens/fungibles/regular.rs`) discards the *actual* amount moved and always reports success for the caller-requested amount, so `pallet-psm::mint` can mint the full internal-asset equivalent even when less external collateral was actually deposited.

### Finding Description
`Pallet::mint` at [1](#0-0)  does:

```
T::Fungibles::transfer(external_asset.clone(), &who, &psm_account, effective_external, Preservation::Expendable)?;
T::Fungibles::mint_into(internal_asset.clone(), &who, internal_to_user)?;
```

The mint amount (`internal_to_user`, derived from `effective_external` computed earlier at [2](#0-1)  ) is minted purely because `transfer(...)` returned `Ok`, under the assumption that `Ok` means "exactly `effective_external` was moved into the reserve account."

That assumption is false for the generic default implementation of `fungibles::Mutate::transfer`: [3](#0-2) 

```rust
fn transfer(asset, source, dest, amount, preservation) -> Result<Self::Balance, DispatchError> {
    let _extra = Self::can_withdraw(asset.clone(), source, amount).into_result(preservation != Expendable)?;
    Self::can_deposit(asset.clone(), dest, amount, Extant).into_result()?;
    if source == dest { return Ok(amount); }
    Self::decrease_balance(asset.clone(), source, amount, BestEffort, preservation, Polite)?;
    // This should never fail as we checked `can_deposit` earlier. But we do a best-effort anyway.
    let _ = Self::increase_balance(asset.clone(), dest, amount, BestEffort);
    Self::done_transfer(asset, source, dest, amount);
    Ok(amount)
}
```

Two unchecked-return-value problems exist here, mirroring the `transferFrom` bug exactly:
1. `decrease_balance(..., BestEffort, ...)` is allowed to withdraw *less* than `amount` from `source` (that's what `BestEffort` precision means) and its `Ok(actual)` result is only propagated for the `?`-error case, never compared to `amount`.
2. `increase_balance(..., BestEffort)`'s return value — the actual amount credited to `dest` (the PSM reserve account) — is thrown away with `let _ = ...`.
3. Regardless of what was really moved, the function unconditionally returns `Ok(amount)` — the *requested* amount, not the *actual* one.

Because `Config::Fungibles` in `pallet-psm` is a generic associated type (`type Fungibles: FungiblesMutate<Self::AccountId, ...>` at [4](#0-3) ), any runtime that wires a fungibles backend which does not override `transfer` (i.e. relies on this default trait method — which is exactly what the trait is for) inherits this gap. `pallet-psm::mint` trusts that `Ok` from `transfer` means the reserve account actually received `effective_external`, then immediately mints the corresponding internal stablecoin with no post-transfer balance verification of the reserve account.

### Impact Explanation
If the underlying `Fungibles` backend can leave `decrease_balance`/`increase_balance` short (e.g. the source account is near a hold/freeze threshold so `BestEffort` withdraws less than requested, or the destination account rejects part of the deposit for backend-specific reasons while `can_deposit`'s earlier snapshot check no longer matches at execution time), a caller can walk away with freshly minted internal stablecoin backed by less external collateral than the pallet's own debt-ceiling accounting (`PsmDebt`) assumes. This breaks the "conserve value / settle exactly once" invariant for a token-minting entry point: PSM debt is recorded as fully collateralized (`PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt)` at [5](#0-4) ) when it is not, leading to an unbacked mint of the stablecoin — the same "theft or unbacked mint" class the report describes.

### Likelihood Explanation
The path is reachable by any unprivileged signed account calling the public `mint` extrinsic with no special permissions (`ensure_signed(origin)?` only). It requires no malicious peer, validator, relayer, or admin — only a `Fungibles` backend configuration that exercises the default trait `transfer` implementation, which is the normal, documented behavior of the trait (not a misconfiguration). This makes it a genuine "public wrapper does not verify inner operation before advancing state" bug rather than a purely theoretical one.

### Recommendation
- Change `pallet-psm::mint`/`redeem` to use the *returned actual amount* from `T::Fungibles::transfer` (currently discarded) and assert it equals the requested `effective_external`/`external_out` before minting or crediting, or switch to a `transfer` variant with `Precision::Exact` semantics enforced end-to-end.
- Fix the shared default implementation in `substrate/frame/support/src/traits/tokens/fungibles/regular.rs` (and the fungible/`regular.rs` sibling) so `decrease_balance`/`increase_balance` actual amounts are compared against the requested `amount`, returning an error (or the true transferred amount) rather than silently returning `Ok(amount)` regardless of what was actually moved.
- Add an explicit post-transfer reserve-balance check in `pallet-psm::mint` before minting internal tokens, analogous to whitelisting/verifying deposits recommended in the original report.

### Proof of Concept
1. Configure a runtime where `pallet-psm::Config::Fungibles` is backed by an asset implementation that relies on the default `fungibles::Mutate::transfer` (i.e. does not override it) and where the caller's account balance for `external_asset` is exactly at, or interacts with, a hold/freeze boundary such that `decrease_balance(..., BestEffort, ...)` withdraws less than `effective_external`.
2. Call `Psm::mint(origin, internal_asset, external_asset, external_amount, max_fee)` with `who` in that boundary state.
3. `T::Fungibles::transfer(...)` returns `Ok(effective_external)` even though the PSM reserve account received less than `effective_external`.
4. `pallet-psm::mint` proceeds to `T::Fungibles::mint_into(internal_asset, &who, internal_to_user)`, crediting the caller the full stablecoin amount, and records `PsmDebt` as fully backed — an unbacked mint of the internal asset relative to the actual reserve.

Note: I was not able to fully verify, within this session, whether every concrete `fungibles::Mutate` implementer used in a real polkadot-sdk runtime (e.g. `pallet-assets`) overrides `transfer` to avoid this exact gap, or whether it is only reachable through custom/lightweight `Fungibles` implementations wired into `pallet-psm`. That distinction determines whether this is exploitable in a specific shipped runtime today versus being a latent trait-level footgun exposed by this new pallet's design — this would need to be checked against the specific runtime's `Config::Fungibles` type in a follow-up session with full build/test access.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L334-337)
```rust
		/// Fungibles implementation for both internal and external assets.
		type Fungibles: FungiblesMutate<Self::AccountId, AssetId = Self::AssetId>
			+ FungiblesMetadataInspect<Self::AccountId>
			+ FungiblesRolesInspect<Self::AccountId>;
```

**File:** substrate/frame/psm/src/lib.rs (L719-730)
```rust
			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
			ensure!(!internal_equivalent.is_zero(), Error::<T>::AmountTooSmallAfterConversion);
			ensure!(internal_equivalent >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let effective_external =
				Self::internal_to_external(internal_equivalent, ext_decimals, internal_decimals)?;

			let fee_rate = MintingFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_equivalent);
			let internal_to_user = internal_equivalent.saturating_sub(fee);
```

**File:** substrate/frame/psm/src/lib.rs (L743-754)
```rust
			let psm_account = Self::psm_account(&internal_asset);
			T::Fungibles::transfer(
				external_asset.clone(),
				&who,
				&psm_account,
				effective_external,
				Preservation::Expendable,
			)?;
			T::Fungibles::mint_into(internal_asset.clone(), &who, internal_to_user)?;
			if !fee.is_zero() {
				T::Fungibles::mint_into(internal_asset.clone(), &info.fee_destination, fee)?;
			}
```

**File:** substrate/frame/psm/src/lib.rs (L756-756)
```rust
			PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt);
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
