## Finding: Unprotected AMM Spot-Price Swap in Snowbridge System-Frontend Tip/Fee Conversion

### Title
Missing slippage protection (`amount_out_min = None`) in `swap_and_burn` allows spot-price manipulation of the AssetConversion pool to mint an inflated Snowbridge relayer reward/tip credit - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
The external report describes an Ichi-Vault style attack where LP-share issuance is priced off a manipulable spot price with no delta/TWAP protection outside a single block, letting an attacker crash the price and steal deposits. The Polkadot SDK analog is in `pallet-snowbridge-system-frontend`'s `swap_and_burn`/`swap_fee_asset_and_burn` functions, reachable through the public, unprivileged `add_tip` and `register_token` extrinsics. These functions call `pallet-asset-conversion`'s `swap_exact_tokens_for_tokens` with the minimum-output parameter hard-coded to `None`, i.e. no slippage/price-bound check at all, and then use the swap's spot-priced output (`ether_gained`) as the authoritative value forwarded to Ethereum/BridgeHub for relayer reward accounting.

### Finding Description
`Pallet::add_tip` (public call index 2) and `Pallet::register_token` (public call index 1) both invoke `Self::swap_fee_asset_and_burn`, which for any non-ether fee asset calls `Self::swap_and_burn`: [1](#0-0) 

The critical line is:
```
let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
    who.clone(),
    swap_path,
    tip_amount,
    None, // No minimum amount required
    who,
    true,
)?;
``` [2](#0-1) 

`pallet-asset-conversion`'s AMM computes the swap output purely from the current pool reserves (constant-product formula) at execution time — there is no TWAP, no delta check, and no minimum-output enforcement here, since the caller explicitly passes `None`: [3](#0-2) 

Because `add_tip` and `register_token` are public, unprivileged, callable-by-anyone extrinsics, an attacker can:
1. Manipulate the relevant `pallet-asset-conversion` pool reserves (e.g. via a preceding swap in the same block/batch, or via `pallet_utility::batch_all` combining a large swap with the `add_tip` call atomically) to temporarily skew the spot exchange rate of `fee_asset_location` → `ether_location`.
2. Call `add_tip` with a fixed `tip_amount` of the manipulated asset; `ether_gained` is computed off the distorted reserves with zero protection.
3. `ether_gained` is burned via `burn_for_teleport` and forwarded in the `AddTip`/`RegisterToken` Transact call to `EthereumSystem` on BridgeHub, which allocates that (attacker-controlled, inflated or deflated) figure as the relayer reward/tip credited on the Ethereum side.

The exact corrupted value is `ether_gained` in `swap_and_burn` — it is trusted downstream as the true economic value of the tip/registration fee, but it is fully attacker-influenceable within a single atomic transaction because no `amount_out_min` bound exists.

### Impact Explanation
This breaks the "public underpriced work / duplicate settlement / wrong beneficiary or amount" impact class: an unprivileged caller can extract value from `pallet-asset-conversion` liquidity providers (classic sandwich extraction, now unprotected by design) while simultaneously causing bridge reward accounting (`AddTip`, relayer rewards recorded on Ethereum) to be settled at an artificial exchange rate instead of the exact amount actually paid in economic terms. This can result in relayers being credited disproportionate rewards relative to what was actually contributed, or liquidity providers in the swap pool being drained through repeated exploitation of this always-present zero-slippage path, which is a "public underpriced work that degrades... bridge processing" and "theft or unbacked... payout" style issue as scoped by the impact gate.

### Likelihood Explanation
High feasibility relative to the original external report: this does not require multi-block MEV, a malicious validator, or controlling two consecutive blocks — it can be done entirely within a single transaction (or single block) by any signed account using ordinary tools like `pallet_utility::batch_all` to sequence a manipulating swap immediately before `add_tip`/`register_token`. No privileged role, governance action, or off-chain infrastructure is required, satisfying the "public entrypoint... unprivileged attacker" requirement of the pivots.

### Recommendation
Never hard-code `None` for the minimum-output/slippage parameter when a swap result feeds into settlement or reward accounting. `swap_and_burn` should compute an acceptable minimum bound (e.g., using `quote_price_exact_tokens_for_tokens` as a reference and enforcing a bounded deviation, or requiring the caller to supply a `min_ether_out` and reverting otherwise), consistent with how ordinary AMM swap extrinsics elsewhere in `pallet-asset-conversion` require callers to supply `amount_out_min`/`amount_in_max`. This mirrors the report's own recommendation: check spot/expected price against an allowed delta regardless of manipulation timing.

### Proof of Concept
1. Attacker sets up (or already exists) a `pallet-asset-conversion` pool for `(fee_asset, ether_location)` with shallow liquidity relative to their available capital.
2. Attacker builds a single extrinsic batch (`pallet_utility::batch_all`) containing:
   - a large `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` call that skews the pool reserves so that `fee_asset → ether` output is temporarily inflated,
   - `snowbridge_pallet_system_frontend::add_tip(message_id, fee_asset_amount)`.
3. Inside `add_tip`, `swap_and_burn` executes the exact-input swap with `None` minimum output against the now-skewed reserves, returning an inflated `ether_gained`.
4. `ether_gained` is burned and forwarded via `AddTip` to BridgeHub/EthereumSystem, crediting the attacker/relayer reward pool with an amount disproportionate to real economic value contributed.
5. The batch's final leg (or a follow-up transaction) reverses the initial swap, restoring the pool and leaving the attacker's net cost near zero while the reward ledger reflects the manipulated, inflated `ether_gained`.

Note: I was unable to directly view the exact signature/body of `swap.rs`'s `Swap` trait implementation for `swap_exact_tokens_for_tokens` (only confirmed via grep that `amount_out_min`-style parameters exist in that file), so the precise parameter-ordering assumption is inferred from the call site and `lib.rs` quote function; a Devin session with full repo access should verify the exact trait signature before finalizing a fix.

### Citations

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1523-1546)
```rust
		pub fn quote_price_exact_tokens_for_tokens(
			asset1: T::AssetKind,
			asset2: T::AssetKind,
			amount: T::Balance,
			include_fee: bool,
		) -> Option<T::Balance> {
			// Swaps reject zero amounts, match that behavior.
			if amount.is_zero() {
				return None;
			}

			let pool_account = T::PoolLocator::pool_address(&asset1, &asset2).ok()?;

			let (balance1, balance2) = Self::get_reserves(asset1.clone(), asset2.clone()).ok()?;

			if balance1.is_zero() {
				return None;
			}

			let amount_out = if include_fee {
				let fee = Self::pool_fee_for(&asset1, &asset2).ok()?;
				Self::get_amount_out(fee, &amount, &balance1, &balance2).ok()?
			} else {
				Self::quote(&amount, &balance1, &balance2).ok()?
```
