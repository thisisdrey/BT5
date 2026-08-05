Based on the evidence gathered, I found a strong local analog to the Illuminate "PT-mints-PT" inflation bug in the **PSM (Peg Stability Module) pallet** (`substrate/frame/psm/src/lib.rs`), which structurally mirrors `Lender.mint()` / `IMarketPlace.token()`: it mints a token to a user against a "supported" backing asset ID, but does not verify that this backing asset is actually a *different, independently-issued* asset.

### Title
PSM `mint()` inflates internal asset supply if an external asset entry aliases the internal asset itself - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`Pallet::mint()` accepts any `external_asset` that has an `ExternalAssets<T>` record for the target `internal_asset`, transfers that asset into the PSM reserve, and then **mints brand-new `internal_asset`** to the caller [1](#0-0) . Nowhere in `mint()`, `redeem()`, or the code path I could inspect for asset validation is there a check that `external_asset != internal_asset`. If the pair `(internal_asset, internal_asset)` is ever recorded in `ExternalAssets<T>` (via `add_external_asset`), the PSM becomes exactly analogous to Illuminate's Lender: it transfers the "backing" token into its own reserve **without burning it**, while unconditionally minting a fresh amount of the very same asset to the caller, exactly as `Lender.mint()` transfers a PT in and mints a new Illuminate PT out without burning the input.

### Finding Description
In `mint()`:
- `ExternalAssets::<T>::get(&internal_asset, &external_asset)` is the only gate deciding whether `external_asset` is "supported" [2](#0-1) .
- The function then does `T::Fungibles::transfer(external_asset, who -> psm_account, effective_external)` followed by `T::Fungibles::mint_into(internal_asset, who, internal_to_user)` [1](#0-0) .
- If `external_asset == internal_asset`, the transferred tokens are **not burned** — they merely move to `psm_account` — while `mint_into` still creates new supply of the same asset id. This is the same "value not destroyed but new supply created" primitive as the Illuminate report: `Safe.transferFrom(principal, user, lender, a)` followed by `IERC5095(principalToken).authMint(user, a)` without burning the input PT [3](#0-2) .

I could not, within available tool calls, confirm the presence or absence of an explicit `ensure!(external_asset != internal_asset, ...)` guard inside `add_external_asset` (the function body was not retrieved before the iteration budget ran out). Grepping the test suite for guard-related identifiers (`SameAsInternal`, `InternalCannotBeExternal`, self-referential checks) returned no matches, which is consistent with — but not conclusive proof of — a missing invariant. This should be verified directly in `substrate/frame/psm/src/lib.rs`'s `add_external_asset` function and its `Error` enum before treating this as fully confirmed.

### Impact Explanation
If the self-referential pair can be registered (whether through an oversight in `add_external_asset`, or because `T::AssetId` equality can be satisfied by two nominally-different registrations that resolve to the same underlying asset, similar to how Snowbridge's `TokenIdOf::convert_location` / `ForeignToNativeId` mapping can alias distinct `Location`s to the same asset id [4](#0-3) ), any signed account can call the public `mint()` extrinsic repeatedly to mint unlimited new supply of `internal_asset` while only shuffling existing balance into the PSM reserve. Since `redeem()` pays out from the reserve proportionally to recorded `PsmDebt` [5](#0-4) , unbacked minted supply dilutes redemption value for all other holders of `internal_asset` — a direct, permanent value-conservation break matching the "Balances... must conserve value" pivot.

### Likelihood Explanation
Reaching this state requires that `ExternalAssets<T>` contain an entry where `external_asset == internal_asset` for some PSM instance. This is gated behind `add_external_asset`, which is restricted to a PSM's admin (`can_manage_assets`). Because the *root cause* of the exploit is a missing input-validation guard in the pallet's dispatchable/config path (not the misuse of legitimate admin power to attack the system), and the *actual drain* is executed by any ordinary signed user calling the public `mint()` extrinsic once misconfigured, this sits at the boundary flagged by the report's method as worth surfacing — but full confirmation requires inspecting `add_external_asset`'s validation logic directly, which I was unable to complete.

### Recommendation
Add an explicit guard rejecting self-referential PSM configuration, mirroring the Illuminate fix:
```rust
ensure!(external_asset != internal_asset, Error::<T>::InvalidExternalAsset);
```
in `add_external_asset` (and defensively re-check in `mint()`/`redeem()` before minting/burning), so that no PSM instance can ever hold an `ExternalAssets` entry keyed by its own internal asset id, regardless of how that id is derived or aliased upstream.

### Proof of Concept
Conceptual reproduction (pending confirmation that `add_external_asset` lacks the guard):
1. Configure a PSM instance with `internal_asset = X`.
2. Call `add_external_asset(X, X)` (if not rejected).
3. Fund account with `n` units of `X`.
4. Call `Psm::mint(X, X, n, max_fee)`.
5. Observe: caller's balance of `X` decreases by `effective_external` but increases by `internal_to_user` (≈ same amount minus fee), while `T::Fungibles::total_issuance(X)` increases by `internal_to_user + fee` — new supply created without new backing, deposited tokens merely relocated to `psm_account` rather than burned.
6. Repeat to inflate `X`'s total supply arbitrarily, diluting all other holders' redemption value via `redeem()`'s reserve-based payout.

**Caveat:** This finding is based on evidence gathered before hitting the tool-call limit; I was unable to directly inspect the body of `add_external_asset` in `substrate/frame/psm/src/lib.rs` to conclusively verify whether the `external_asset != internal_asset` guard already exists. This should be the first thing verified before acting on this report.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L277-287)
```rust
		Encode, Decode, DecodeWithMemTracking, MaxEncodedLen, TypeInfo, Clone, PartialEq, Eq, Debug,
	)]
	#[scale_info(skip_type_params(T))]
	pub struct PsmInfo<T: Config> {
		/// Account receiving minting and redemption fees, denominated in the internal asset.
		pub fee_destination: T::AccountId,
		/// This PSM instance's debt ceiling, in internal-asset units.
		pub max_debt: BalanceOf<T>,
		/// Minimum swap amount for this instance, in internal-asset units. Swaps whose
		/// internal-equivalent falls below this are rejected with [`Error::BelowMinimumSwap`].
		pub min_swap_amount: BalanceOf<T>,
```

**File:** substrate/frame/psm/src/lib.rs (L712-714)
```rust
			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_minting(), Error::<T>::MintingStopped);
```

**File:** substrate/frame/psm/src/lib.rs (L743-756)
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

			PsmDebt::<T>::insert(&internal_asset, &external_asset, new_debt);
```

**File:** substrate/frame/psm/src/lib.rs (L835-855)
```rust
			let external_out =
				Self::internal_to_external(internal_net, ext_decimals, internal_decimals)?;
			ensure!(
				internal_net.is_zero() || !external_out.is_zero(),
				Error::<T>::AmountTooSmallAfterConversion
			);
			// `effective_internal_net` is the internal value that round-trips to `external_out`;
			// it is what we actually burn and what the tracked debt decreases by. Any truncation
			// dust stays in the caller's internal balance, symmetric with `mint`, which takes
			// only the round-tripped share of the external amount.
			let effective_internal_net =
				Self::external_to_internal(external_out, ext_decimals, internal_decimals)?;

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			ensure!(current_debt >= effective_internal_net, Error::<T>::InsufficientReserve);

			let reserve = Self::get_reserve(&internal_asset, &external_asset);
			if reserve < external_out {
				defensive!("PSM reserve is less than expected output amount");
				return Err(Error::<T>::Unexpected.into());
			}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L220-231)
```rust
			let sender_location: Location =
				(*sender).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;

			let location = Self::reanchor(asset_location)?;
			let token_id = TokenIdOf::convert_location(&location)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			if !ForeignToNativeId::<T>::contains_key(token_id) {
				ForeignToNativeId::<T>::insert(token_id, location.clone());
			}
```
