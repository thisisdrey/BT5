## Analysis Summary

The IPSeed bug's core invariant is: **a public entry point lets an attacker configure state that is *insufficiently backed/authorized*, mint against that state, and later realize real value from it, draining protocol-held funds — because a supposedly-privileged action was gated on the wrong check.**

The closest verifiable local analog is in the new `pallet-psm` (Peg Stability Module) added to this fork, specifically in how `create_psm`'s origin check conflates the `pallet-assets` **`owner`** role with the **`issuer`** role, combined with `add_external_asset` not rejecting a PSM registering itself as its own external asset.

### Title
Unauthorized unbacked mint via `pallet-psm` self-referential PSM and owner/issuer role confusion - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet_psm::EnsureAssetOwner` authorizes `create_psm` for any signed account that is the `owner` of the target asset in `pallet-assets`, per [1](#0-0) . In `pallet-assets`, `owner` is a distinct role from `issuer` (the role gated by the extrinsic-level check in `mint`) — an account can hold `owner` without holding `issuer`, e.g. after a partial role split via `set_team`/`transfer_ownership`. `pallet_psm::mint` calls `T::Fungibles::mint_into` directly (the trait method, not the `pallet-assets` extrinsic), so it never re-checks the `issuer` role, only whatever `pallet-psm` itself enforces [2](#0-1) . Additionally, `add_external_asset` never checks `external_asset != internal_asset` [3](#0-2) , so a PSM can be created with its own internal asset registered as its own approved external.

### Finding Description
1. Attacker becomes `owner` (but not `issuer`) of some existing asset `X` (a legitimately reachable state via `pallet-assets` team/ownership management, not requiring governance or key compromise).
2. Attacker calls `create_psm(internal_asset = X, ...)`. `EnsureAssetOwner::try_origin` only checks `Fungibles::owner(X) == Some(attacker)` [1](#0-0) , so this succeeds even though the attacker cannot call `pallet_assets::mint` directly (that requires `issuer`).
3. Attacker calls `add_external_asset(internal_asset = X, external_asset = X)`. No check prevents `external_asset == internal_asset` [4](#0-3) ; only existence and decimals-match checks are performed, which trivially pass for the same asset.
4. Attacker, as `full_admin` of their own PSM instance, calls `set_asset_ceiling_weight` to remove the default zero-weight block [5](#0-4) .
5. Attacker repeatedly calls `mint(internal_asset = X, external_asset = X, external_amount = A, ...)`. This transfers `A` of `X` from the attacker to the PSM's own reserve account, and then unconditionally calls `T::Fungibles::mint_into(X, &attacker, internal_to_user)` [2](#0-1)  — creating *new* supply of `X` and crediting it to the attacker, net of a small fee that the attacker also controls (`fee_destination` is attacker-chosen at `create_psm`).
6. Because `mint_into` is called at the trait level, the `issuer`-only guard normal `pallet-assets::mint` extrinsic enforces is completely bypassed. The attacker mints new supply of `X` despite not holding `issuer`.
7. Newly minted `X` can be sold into any real liquidity venue (e.g. `pallet_asset_conversion` pools) for genuine value, draining that pool's counter-asset.

### Impact Explanation
This is an unauthorized/unbacked mint: an account with only the `owner` role on an asset gains the practical minting power reserved for `issuer`, and can inflate that asset's supply against a self-referential, non-genuine collateral relationship. If the asset in question has any real liquidity (AMM pool, bridge-wrapped representation, etc.), the attacker can realize real value by dumping freshly minted supply, i.e. theft/unbacked mint and pool drain — matching the "theft or unbacked mint" and "runtime bugs that compromise intended behavior" impact categories.

### Likelihood Explanation
High for any deployment that instantiates `pallet-psm` with `EnsureAssetOwner` as `CreateOrigin` (as both `substrate/bin/node/runtime` and `asset-hub-westend` do [6](#0-5) [7](#0-6) ). No admin/governance/validator/relayer compromise is needed — only that `owner` and `issuer` roles for some target asset have diverged, a normal, supported configuration in `pallet-assets` team management. All of `create_psm`, `add_external_asset`, `set_asset_ceiling_weight`, and `mint` are plain signed-origin dispatchables.

### Recommendation
- In `add_external_asset`, reject `external_asset == internal_asset`.
- In `EnsureAssetOwner`, additionally require the caller to hold `issuer` (or explicitly document/require that `mint_into`/`burn_from` calls in `pallet_psm::mint`/`redeem` are safe regardless of the caller's `pallet-assets` role — which they are not, since they bypass the issuer check entirely). At minimum, gate `pallet_psm::mint`'s `mint_into` call on the PSM's own collateral actually coming from a *distinct* external asset, and verify the internal asset's `issuer` matches the PSM's expectations at `create_psm` time (and re-validate if roles change).

### Proof of Concept
```
// 1. Attacker holds `owner` (not `issuer`) role of asset X, e.g. via prior
//    pallet_assets::set_team splitting roles, or transfer_ownership.
Psm::create_psm(signed(attacker), X, Box::new(attacker_origin), Box::new(attacker_origin),
                 attacker /* fee_destination */, u128::MAX /* max_debt */, 1);

// 2. Register X as its own external asset — no self-reference check exists.
Psm::add_external_asset(signed(attacker), X, X);

// 3. Attacker is full_admin of their own instance — lift the ceiling.
Psm::set_asset_ceiling_weight(signed(attacker), X, X, Permill::from_percent(100));

// 4. Attacker owns some initial X (however small) and loops:
loop {
    Psm::mint(signed(attacker), X, X, A, Permill::from_percent(100));
    // -> transfers A of X: attacker -> psm reserve
    // -> mint_into(X, attacker, ~A - fee): brand-new X supply credited to attacker
    // Net: attacker's spendable X supply grows every iteration without ever
    // holding pallet_assets `issuer` rights on X.
}
```

### Citations

**File:** substrate/frame/psm/src/lib.rs (L394-404)
```rust
		fn try_origin(
			origin: <T as frame_system::Config>::RuntimeOrigin,
			internal_asset: &T::AssetId,
		) -> Result<Self::Success, <T as frame_system::Config>::RuntimeOrigin> {
			match ensure_signed(origin.clone()) {
				Ok(who) if T::Fungibles::owner(internal_asset.clone()) == Some(who.clone()) => {
					Ok(Some(who))
				},
				_ => Err(origin),
			}
		}
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

**File:** substrate/frame/psm/src/lib.rs (L1260-1282)
```rust
		pub fn set_asset_ceiling_weight(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			weight: Permill,
		) -> DispatchResult {
			Self::ensure_psm_admin(origin, &internal_asset, |l| l.can_set_asset_ceiling())?;
			ensure!(
				ExternalAssets::<T>::contains_key(&internal_asset, &external_asset),
				Error::<T>::AssetNotApproved
			);
			// Reweighting renormalises every external's ceiling; an external left below its new
			// ceiling simply can't be minted until redemptions bring its debt back down.
			let old_value = AssetCeilingWeight::<T>::get(&internal_asset, &external_asset);
			AssetCeilingWeight::<T>::insert(&internal_asset, &external_asset, weight);
			Self::deposit_event(Event::AssetCeilingWeightUpdated {
				internal_asset,
				external_asset,
				old_value,
				new_value: weight,
			});
			Ok(())
		}
```

**File:** substrate/frame/psm/src/lib.rs (L1316-1356)
```rust
		pub fn add_external_asset(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
		) -> DispatchResult {
			Self::ensure_psm_admin(origin, &internal_asset, |l| l.can_manage_assets())?;
			let mut info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;
			ensure!(
				!ExternalAssets::<T>::contains_key(&internal_asset, &external_asset),
				Error::<T>::AssetAlreadyApproved
			);
			ensure!(info.external_count < T::MaxExternals::get(), Error::<T>::TooManyAssets);
			ensure!(
				T::Fungibles::asset_exists(external_asset.clone()),
				Error::<T>::AssetDoesNotExist
			);

			let asset_decimals = T::Fungibles::decimals(external_asset.clone());
			ensure!(
				T::Fungibles::decimals(internal_asset.clone()) == info.internal_decimals,
				Error::<T>::DecimalsMismatch
			);
			ensure!(
				(asset_decimals.abs_diff(info.internal_decimals) as u32) <= MAX_DECIMALS_DIFF,
				Error::<T>::DecimalsRangeExceeded
			);

			ExternalAssets::<T>::insert(
				&internal_asset,
				&external_asset,
				ExternalAssetInfo {
					status: CircuitBreakerLevel::AllEnabled,
					decimals: asset_decimals,
				},
			);
			info.external_count = info.external_count.saturating_add(1);
			Psm::<T>::insert(&internal_asset, info);

			Self::deposit_event(Event::ExternalAssetAdded { internal_asset, external_asset });
			Ok(())
		}
```

**File:** substrate/bin/node/runtime/src/lib.rs (L3214-3214)
```rust
	type CreateOrigin = pallet_psm::EnsureAssetOwner<Runtime>;
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1570-1573)
```rust
type PsmCreateOrigin = EitherOf<
	pallet_psm::EnsureAssetOwner<Runtime>,
	EnsureRootWithSuccess<AccountId, NoPsmDepositor>,
>;
```
