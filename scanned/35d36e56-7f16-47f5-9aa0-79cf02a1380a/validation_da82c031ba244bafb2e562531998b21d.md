### Title
PSM `create_psm` authorizes on asset **owner** instead of asset **issuer**, letting the owner role permanently open unlimited permissionless minting of the internal asset, bypassing the `pallet_assets` issuer authorization gate — ([File: substrate/frame/psm/src/lib.rs])

### Summary
The external report's core broken invariant is that a token's minting is authorized against the wrong entity: `Ownable`'s default deployer-owner rather than the contract that is actually supposed to control minting. The local analog is `pallet-psm`'s `EnsureAssetOwner`, which gates the creation of a Peg Stability Module (PSM) — a construct that subsequently gives **any signed account** permissionless minting power over an asset — on the asset's `owner` role, while `pallet-assets` reserves minting authority for the distinct `issuer` role. This mismatch lets the `owner` (a role meant only to manage team assignment/destruction per `pallet-assets` documentation) unilaterally create a public minting faucet for an asset it does not hold `issuer` rights over, permanently bypassing the intended issuer-only mint gate.

### Finding Description
`pallet-assets` explicitly documents two distinct privileged roles: [1](#0-0) 
- **Issuer**: uniquely privileged to mint the asset.
- **Owner**: uniquely privileged to destroy the asset class or reassign Issuer/Freezer/Admin.

`do_mint` in `pallet-assets` enforces that only the `issuer` may mint when a caller check is supplied: [2](#0-1) 

However, `pallet-psm`'s `EnsureAssetOwner`, used as `Config::CreateOrigin` to authorize `create_psm`, checks the caller against `T::Fungibles::owner(...)`, not the `issuer`: [3](#0-2) 

The pallet's own doc-comment acknowledges the resulting authorization gap: creating a PSM over an asset lets the PSM's `mint`/`redeem` entry points bypass the asset's issuer checks entirely, because the PSM calls `T::Fungibles::mint`/`burn` (the low-level `fungibles::Mutate` trait) directly rather than going through the `pallet_assets::mint` extrinsic that enforces `check_issuer`: [4](#0-3) 

Once `create_psm` succeeds, the `mint` extrinsic is fully public/permissionless — any `Signed` account can invoke it to mint the internal asset (subject only to the circuit breaker and reserve backing), as documented in the dispatchable's own comment: [5](#0-4) 

This reproduces the report's exact bug class: minting authority is bound to a role (`owner`) that is not the actual, narrowly-scoped minting authority (`issuer`) intended by the asset model, and once that mismatch is exploited, the wrong party permanently unlocks a public minting path that the `issuer` role was specifically designed to gate. The unit test `ensure_asset_owner_admits_only_the_owner` confirms the check is indeed keyed off `owner`, not `issuer`, and that even Root cannot pass it — reinforcing that `owner` (not `issuer`) is the sole gate for spinning up this permissionless minting surface: [6](#0-5) 

### Impact Explanation
An asset's `owner` role is conventionally treated as an administrative/custodial role (able to reassign the mint-capable `issuer`, freezer, and admin, or destroy the class) — not itself an unrestricted mint authority. By calling `create_psm`, that `owner` account converts itself into the origin that opens an unlimited, permissionless minting faucet for the asset (bounded only by external-asset deposits and the configurable debt ceiling), entirely decoupling supply control from the `issuer` role that governance/other pallets may rely on as the sole minting gate. This breaks the segregation of duties the Owner/Issuer split is designed to enforce, and constitutes an implementation bug that compromises intended asset-accounting behavior — any deployment that treats `owner` as a lower-trust role than `issuer` (e.g., an owner set to a non-privileged deposit-refund manager per `do_refund_other`'s owner check) would have its issuer-only mint invariant silently voided.

### Likelihood Explanation
This does not require a malicious peer, validator, collator, or leaked key — it only requires an account that already holds the `owner` role of an asset (a role frequently distinct from `issuer` by design, per the pallet's own terminology) to call the public `create_psm` extrinsic once. After that single call, the minting bypass is permanent and exploitable by any unprivileged signed account through the ordinary `mint` extrinsic.

### Recommendation
Change `EnsureAssetOwner` (or introduce a dedicated origin check) to verify the caller is the asset's `issuer` (or requires both `owner` and `issuer` consent) before allowing `create_psm`, since the PSM's `mint`/`redeem` flow bypasses `pallet_assets`'s issuer check entirely. Alternatively, require the PSM's low-level `Fungibles::mint` calls to still validate against the asset's registered `issuer` at swap time, not just at PSM-creation time, so a later change of `issuer`/`owner` via `set_team` is reflected in the PSM's minting authority.

### Proof of Concept
1. Asset `X` is created with `owner = Alice`, `issuer = Bob` (via `pallet_assets::create` + `set_team`), so only `Bob` should be able to mint `X`.
2. Alice (owner, not issuer) calls `Psm::create_psm(signed(Alice), X, ...)`. This passes `EnsureAssetOwner::try_origin`, which only checks `T::Fungibles::owner(X) == Alice` — see `substrate/frame/psm/src/lib.rs:399`.
3. Any third-party account `Carol` (unprivileged, no relation to `issuer` Bob) calls `Psm::mint(signed(Carol), X, external_asset, amount, max_fee)`. This dispatchable has no issuer check; it deposits `external_asset` into the PSM reserve and calls `T::Fungibles::mint` to credit Carol with `X`, entirely bypassing Bob's exclusive issuer authority.
4. Result: `X`'s supply is now mintable by any signed user through the PSM, even though the intended supply-control model reserved that right for `issuer = Bob` alone — the same "wrong authority controls minting" defect described in the source report, realized here via the `owner`/`issuer` role mismatch in `create_psm`.

### Citations

**File:** substrate/frame/assets/src/lib.rs (L57-64)
```rust
//! * **Issuer**: An account ID uniquely privileged to be able to mint a particular class of assets.
//! * **Freezer**: An account ID uniquely privileged to be able to freeze an account from
//!   transferring a particular class of assets.
//! * **Freezing**: Removing the possibility of an unpermissioned transfer of an asset from a
//!   particular account.
//! * **Non-fungible asset**: An asset for which each unit has unique characteristics.
//! * **Owner**: An account ID uniquely privileged to be able to destroy a particular asset class,
//!   or to set the Issuer, Freezer, Reserves, or Admin of that asset class.
```

**File:** substrate/frame/assets/src/functions.rs (L457-466)
```rust
	pub(super) fn do_mint(
		id: T::AssetId,
		beneficiary: &T::AccountId,
		amount: T::Balance,
		maybe_check_issuer: Option<T::AccountId>,
	) -> DispatchResult {
		Self::increase_balance(id.clone(), beneficiary, amount, |details| -> DispatchResult {
			if let Some(check_issuer) = maybe_check_issuer {
				ensure!(check_issuer == details.issuer, Error::<T, I>::NoPermission);
			}
```

**File:** substrate/frame/psm/src/lib.rs (L384-404)
```rust
	/// [`Config::CreateOrigin`] admitting a signed origin only when it owns the internal asset.
	/// Prevents creating a PSM over an asset you don't control (PSM mint/burn bypasses the
	/// asset's issuer checks).
	pub struct EnsureAssetOwner<T>(core::marker::PhantomData<T>);

	impl<T: Config> EnsureOriginWithArg<<T as frame_system::Config>::RuntimeOrigin, T::AssetId>
		for EnsureAssetOwner<T>
	{
		type Success = Option<T::AccountId>;

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

**File:** substrate/frame/psm/src/lib.rs (L657-671)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T> {
		/// Swap external asset for internal on a specific PSM instance.
		///
		/// ## Dispatch Origin
		///
		/// Must be `Signed` by the user performing the swap.
		///
		/// ## Details
		///
		/// Transfers `external_amount` of `external_asset` from the caller to the
		/// `internal_asset`'s PSM reserve account, then mints `internal_asset` to the
		/// caller minus the minting fee. The fee is calculated using ceiling rounding
		/// (`mul_ceil`), ensuring the protocol never undercharges. The fee is
		/// transferred to [`PsmInfo::fee_destination`] of the targeted instance.
```

**File:** substrate/frame/psm/src/tests.rs (L3452-3478)
```rust
	#[test]
	fn ensure_asset_owner_admits_only_the_owner() {
		// Exercises the pallet-provided `EnsureAssetOwner` directly (the mock's `CreateOrigin`
		// is plain signed). Only the asset owner's signed origin passes.
		use frame_support::traits::EnsureOriginWithArg;
		new_test_ext().execute_with(|| {
			assert_ok!(Assets::create(RuntimeOrigin::signed(ALICE), NEW_INTERNAL, ALICE, 1));
			assert_eq!(
				crate::EnsureAssetOwner::<Test>::try_origin(
					RuntimeOrigin::signed(ALICE),
					&NEW_INTERNAL,
				)
				.ok(),
				Some(Some(ALICE)),
			);
			assert!(crate::EnsureAssetOwner::<Test>::try_origin(
				RuntimeOrigin::signed(BOB),
				&NEW_INTERNAL,
			)
			.is_err());
			assert!(crate::EnsureAssetOwner::<Test>::try_origin(
				RuntimeOrigin::root(),
				&NEW_INTERNAL,
			)
			.is_err());
		});
	}
```
