This is the exact analog of the M-10 pattern: a strict equality check against a "canonical" location used to decide whether to skip a swap step, in `swap_fee_asset_and_burn` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs`.

### Title
Strict location equality check in `swap_fee_asset_and_burn` fails to recognize reserve/foreign representations of Ether, causing tip/fee processing to attempt an invalid swap and revert - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`swap_fee_asset_and_burn` decides whether a user-supplied fee/tip asset needs to be swapped into Ether by comparing `fee_asset_location` against `T::EthereumLocation::get()` with a strict `!=` check [1](#0-0) . Just like the CurveConvex `_executeRedemptionTrades` bug, which compared `address(tokens[i]) == address(asset)` and failed to recognize that ETH (post-wrap) is equivalent to WETH, this Rust code assumes the caller's `Asset.id` location is byte-for-byte identical to the pallet's canonical `EthereumLocation` whenever the asset is "already Ether." If a caller supplies an Ether-denominated asset using any location that is XCM-equivalent to `EthereumLocation` but not byte-identical (e.g., differing junction encoding across XCM versions, or a locally-reserve-backed vs. globally-anchored Ether representation), the equality check incorrectly treats it as a foreign asset that must be swapped.

### Finding Description
`register_token` and `add_tip` are public, unprivileged extrinsics [2](#0-1) . Both delegate fee handling to `swap_fee_asset_and_burn`, which pattern-matches the fee asset and then does a raw `!=` comparison of `Location` values to decide the path:

```rust
let ether_gained = if *fee_asset_location != ether_location {
    Self::swap_and_burn(...)?
} else {
    burn_for_teleport::<T::AssetTransactor>(&origin, &fee_asset)...
    fee_amount
};
```

This mirrors exactly the flawed pattern in the Solidity report: instead of consulting a canonical/reanchored form or a semantic equivalence check (analogous to using `PRIMARY_INDEX` instead of raw address comparison), the code relies on raw structural equality of a `Location` against `EthereumLocation`. If the fee asset is passed with an Ether location that is logically the same currency but structurally different (a legitimate scenario since XCM locations can express the same consensus system in different but valid encodings depending on version/depth, and Ether is treated distinctly on AH between its "reserve" form and its canonical `GlobalConsensus(Ethereum)` form used elsewhere in this same codebase, see the `ether_location` handling difference between `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs` and outbound converters), the pallet takes the swap branch (`swap_and_burn`) rather than the burn-for-teleport branch.

`swap_and_burn` then performs `T::Swap::swap_exact_tokens_for_tokens(who, [tip_asset_location, ether_location], tip_amount, ...)` [3](#0-2) . Attempting to swap an asset for "itself" (Ether-for-Ether via `AssetConversion`) is explicitly rejected by the underlying AMM: `pallet_asset_conversion` treats identical assets in a swap path as `Error::InvalidAssetPair` (see the same-asset-pair guard in `substrate/frame/asset-conversion/src/tests.rs`, `can_not_swap_same_asset`). Since no pool exists between Ether and itself, the swap call returns an error and the whole extrinsic (`register_token` or `add_tip`) reverts.

The critical distinction from the Solidity bug is severity direction: here the miscomparison causes an unnecessary swap attempt (not skipping one), but the root cause is identical — an equality check meant to detect "this is already the target asset" that is defeated by two different-but-equivalent representations of the same underlying value.

### Impact Explanation
Because `register_token` and `add_tip` are open to any signed/XCM origin without needing a malicious peer, validator, or admin, this can be triggered by any ordinary user supplying an Ether-denominated fee/tip asset in a non-canonical (but standards-valid) location form. The extrinsic reverts, meaning:
- Legitimate `register_token` calls can be blocked, preventing registration of a Polkadot-native asset for bridging to Ethereum.
- Legitimate `add_tip` calls can be blocked, preventing users from boosting relayer rewards for stuck messages, which can stall bridge message delivery (public underpriced work / stalled bridge processing scenario explicitly in scope).

This does not cause fund loss directly since `withdraw_fee`/`burn_for_teleport` occurs after the branch decision, but it is a denial-of-service on a public entrypoint central to Snowbridge's fee/tip and token-registration flow, degrading bridge processing exactly as called out in the Impact Gate ("public underpriced work that degrades block production or stalls bridge processing").

### Likelihood Explanation
Likelihood depends on whether callers can practically supply an Ether asset location that is XCM-equivalent to, but not identical to, `T::EthereumLocation::get()`. This is plausible because:
- `VersionedLocation`/`Location` conversions elsewhere in the same codebase distinguish multiple valid encodings of "Ether" (e.g., `Location::new(2, [GlobalConsensus(Ethereum)])` used in inbound/outbound converters vs. any locally reserve-anchored representation a wallet or router might produce).
- The check is a raw `PartialEq` on `Location`, with no reanchoring or canonicalization step before comparison, unlike other paths in the same file (`reanchored`) that explicitly reanchor locations before further processing.

I was unable to fully confirm from the available code whether every practical caller-supplied Ether `Asset` would always arrive pre-canonicalized by an upstream XCM executor step before reaching `swap_fee_asset_and_burn` — the pallet's `Config` does not show an explicit reanchoring of `fee_asset`/`asset` prior to this call in the snippets read. This is the main uncertainty in confirming exploitability strength versus a purely defensive/theoretical path.

### Recommendation
Do not rely on raw structural equality between `fee_asset_location` and `EthereumLocation`. Instead:
1. Reanchor/canonicalize `fee_asset_location` (as is already done for other locations via `Self::reanchored`) before comparing to `ether_location`.
2. Or use a semantic equivalence check (e.g., match on the `Junctions`/interior structure, similar to how `convert_token_address` in `bridges/snowbridge/primitives/inbound-queue/src/v1.rs` matches on `token == H160([0;20])` in a canonical form) rather than comparing potentially-non-canonical `Location` values directly.
3. Add explicit unit tests supplying Ether in multiple valid but structurally different location encodings to `register_token`/`add_tip` to confirm the burn-for-teleport branch is taken instead of the swap branch.

### Proof of Concept
1. Configure a chain with `system-frontend` pallet where `T::EthereumLocation::get()` returns `Location::new(2, [GlobalConsensus(Ethereum{chain_id})])`.
2. Call `add_tip(origin, message_id, asset)` where `asset.id` is an `AssetId` wrapping a `Location` that is XCM-equivalent to Ether (e.g., produced via a different reanchoring path or version, but ultimately resolving to the same consensus system) yet not byte-identical to the pallet's stored `EthereumLocation`.
3. `swap_fee_asset_and_burn` evaluates `*fee_asset_location != ether_location` as `true`, entering `swap_and_burn`.
4. `swap_and_burn` calls `T::Swap::swap_exact_tokens_for_tokens(who, [fee_asset_location, ether_location], ...)`. Because both ends of the "path" represent the same underlying asset, `pallet_asset_conversion` rejects this as `Error::InvalidAssetPair` (mirrored by the `can_not_swap_same_asset` test at `substrate/frame/asset-conversion/src/tests.rs:2281-2331`).
5. `swap_and_burn` returns an error, `swap_fee_asset_and_burn` propagates it, and `add_tip`/`register_token` reverts with `Error::SwapError`, denying the caller's otherwise legitimate operation.

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L217-273)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(
			T::WeightInfo::register_token()
				.saturating_add(T::BackendWeightInfo::transact_register_token())
				.saturating_add(T::BackendWeightInfo::do_process_message())
				.saturating_add(T::BackendWeightInfo::commit_single())
				.saturating_add(T::BackendWeightInfo::submit_delivery_receipt())
		)]
		pub fn register_token(
			origin: OriginFor<T>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			fee_asset: Asset,
		) -> DispatchResult {
			ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);

			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let origin_location = T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)?;

			let ether_gained = if origin_location.is_here() {
				// Root origin/location does not pay any fees/tip.
				0
			} else {
				Self::swap_fee_asset_and_burn(origin_location.clone(), fee_asset)?
			};

			let call = Self::build_register_token_call(
				origin_location.clone(),
				asset_location,
				metadata,
				ether_gained,
			)?;

			Self::send_transact_call(origin_location, call)
		}

		/// Add an additional relayer tip for a committed message identified by `message_id`.
		/// The tip asset will be swapped for ether.
		#[pallet::call_index(2)]
		#[pallet::weight(
			T::WeightInfo::add_tip()
				.saturating_add(T::BackendWeightInfo::transact_add_tip())
		)]
		pub fn add_tip(origin: OriginFor<T>, message_id: MessageId, asset: Asset) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: Into<Location>,
		{
			let who = ensure_signed(origin)?;

			let ether_gained = Self::swap_fee_asset_and_burn(who.clone().into(), asset)?;

			// Send the tip details to BH to be allocated to the reward in the Inbound/Outbound
			// pallet
			let call = Self::build_add_tip_call(who.clone(), message_id.clone(), ether_gained);
			Self::send_transact_call(who.into(), call)
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L290-317)
```rust
		fn swap_and_burn(
			origin: Location,
			tip_asset_location: Location,
			ether_location: Location,
			tip_amount: u128,
		) -> Result<u128, DispatchError> {
			// Swap tip asset to ether
			let swap_path = vec![tip_asset_location.clone(), ether_location.clone()];
			let who = T::AccountIdConverter::convert_location(&origin)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
				who.clone(),
				swap_path,
				tip_amount,
				None, // No minimum amount required
				who,
				true,
			)?;

			// Burn the ether
			let ether_asset = Asset::from((ether_location.clone(), ether_gained));

			burn_for_teleport::<T::AssetTransactor>(&origin, &ether_asset)
				.map_err(|_| Error::<T>::BurnError)?;

			Ok(ether_gained)
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L372-404)
```rust
		fn swap_fee_asset_and_burn(
			origin: Location,
			fee_asset: Asset,
		) -> Result<u128, DispatchError> {
			let ether_location = T::EthereumLocation::get();
			let (fee_asset_location, fee_amount) = match fee_asset {
				Asset { id: AssetId(ref loc), fun: Fungible(amount) } => (loc, amount),
				_ => {
					tracing::debug!(target: LOG_TARGET, ?fee_asset, "error matching fee asset");
					return Err(Error::<T>::UnsupportedAsset.into());
				},
			};
			if fee_amount == 0 {
				return Ok(0);
			}

			let ether_gained = if *fee_asset_location != ether_location {
				Self::swap_and_burn(
					origin.clone(),
					fee_asset_location.clone(),
					ether_location,
					fee_amount,
				)
				.inspect_err(|&e| {
					tracing::debug!(target: LOG_TARGET, ?e, "error swapping asset");
				})?
			} else {
				burn_for_teleport::<T::AssetTransactor>(&origin, &fee_asset)
					.map_err(|_| Error::<T>::BurnError)?;
				fee_amount
			};
			Ok(ether_gained)
		}
```
