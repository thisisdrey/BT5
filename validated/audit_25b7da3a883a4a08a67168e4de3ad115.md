### Title
Unenforced anti-spam minimum on user-payable `register_token` lets fee truncate to near-zero, enabling free BridgeHub message spam - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
The original report's core broken invariant is: a fee computed via integer-division/conversion between assets of different value/decimals can legitimately truncate to `0` (or a negligible amount), defeating an anti-spam/anti-abuse fee floor that was designed assuming non-truncated pricing. The local analog is in Snowbridge's `snowbridge-pallet-system-frontend::register_token`, the user-facing (non-root) entry point for registering a Polkadot-native asset as a wrapped ERC20 on Ethereum.

### Finding Description
The privileged/root path in `snowbridge-pallet-system`, `set_token_transfer_fees`, explicitly enforces an anti-spam floor on the ether fee charged for `RegisterForeignToken`: [1](#0-0) 

```
// Basic validation of new costs. Particularly for token registration, we want to ensure
// its relatively expensive to discourage spamming. Like at least 100 USD.
ensure!(
    create_asset_xcm > 0 && transfer_asset_xcm > 0 && register_token > meth(100),
    Error::<T>::InvalidTokenTransferFees
);
```

The intent is explicit in the comment: registering a token on the Ethereum gateway must cost "at least 100 USD" worth of ether to discourage spamming (this is the anti-spam analog of the LiquidityPool `gasFee` design).

However, the *unprivileged* user-facing entry point, `EthereumSystemFrontend::register_token`, does not go through this check at all. It accepts an arbitrary `fee_asset: Asset` chosen by the caller, converts/swaps it into ether via `swap_fee_asset_and_burn`, and forwards whatever `ether_gained` results directly as the `amount` argument of the `RegisterToken` transact call to BridgeHub — with no comparison against the 100 USD-equivalent floor that the privileged path enforces: [2](#0-1) 

```
/// Initiates the registration for a Polkadot-native token as a wrapped ERC20 token on
/// Ethereum.
/// ...
/// All origins are allowed, however `asset_id` must be a location nested within the origin
/// consensus system.
#[pallet::call_index(1)]
...
pub fn register_token(
    origin: OriginFor<T>,
    asset_id: Box<VersionedLocation>,
    metadata: AssetMetadata,
    fee_asset: Asset,
) -> DispatchResult {
    ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);
    ...
    let ether_gained = if origin_location.is_here() {
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
```

`swap_fee_asset_and_burn`/`swap_and_burn` performs `T::Swap::swap_exact_tokens_for_tokens` on the caller-supplied `tip_amount` (derived from `fee_asset`), which is integer-based and can produce a truncated/rounded-down output for small input amounts or thin liquidity pools — exactly the same class of arithmetic truncation as `gasFee = totalGasUsed * tokenGasPrice` rounding to `0` in the original report. Because no `ensure!(ether_gained >= MIN)` guard exists on this path (unlike the sibling privileged pallet), a caller can supply a minimal `fee_asset` amount, get `ether_gained` truncated to a negligible value (even `0`, since `ether_gained` is only used as an `amount` field passed onward, not validated for non-zero), and still successfully dispatch the `RegisterForeignToken` transact command to BridgeHub/Ethereum.

### Impact Explanation
This breaks the explicitly documented invariant that token registration "should be relatively expensive to discourage spamming." An attacker can repeatedly call `register_token` from AssetHub with different `asset_id`s while paying only dust-level fees, causing the outbound queue on BridgeHub to be filled with cheap `RegisterForeignToken` commands and Ethereum gateway transactions the relayer/protocol must process, degrading bridge throughput and burdening BridgeHub's message queue and the Ethereum gateway with underpriced work — matching the "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Likelihood Explanation
The entry point is explicitly open to all unprivileged origins ("All origins are allowed"), requires no governance or admin action, and no malicious peer/relayer/validator assumption — it is a straightforward user-callable extrinsic. The only barrier is whether the swap mechanism can be driven to a near-zero/truncated output, which is inherent to integer-based swap pricing for small amounts or newly created low-liquidity pools, making this practically reachable by any account holding minimal transferable balance.

### Recommendation
Enforce the same anti-spam floor used in the privileged path (`register_token > meth(100)`) on the `ether_gained` value in `snowbridge-pallet-system-frontend::register_token` before constructing/sending the `RegisterToken` transact call, rejecting the call if the converted/swapped ether amount falls below the configured minimum, instead of forwarding a potentially truncated or zero amount.

### Proof of Concept
1. Attacker holds a small quantity of any fungible asset acceptable as `fee_asset` (or a newly created low-liquidity asset pool with the fee/ether pair).
2. Attacker calls `EthereumSystemFrontend::register_token(origin, asset_id, metadata, fee_asset)` with a `fee_asset` amount deliberately small enough that `swap_exact_tokens_for_tokens` inside `swap_fee_asset_and_burn` rounds the output ether amount down to near-zero (or the minimum tradable unit).
3. `ether_gained` (truncated/negligible) is forwarded unchecked as `amount` in `build_register_token_call` and dispatched via `send_transact_call` to BridgeHub's `EthereumSystem::register_token`, without any comparison to the intended 100-USD-equivalent anti-spam floor enforced only on the separate root-only `set_token_transfer_fees`/`register_token` path.
4. Repeat with many distinct `asset_id`s to spam BridgeHub's outbound queue and Ethereum gateway with cheap `RegisterForeignToken` commands.

Note: I was not able to fully inspect the body of `swap_fee_asset_and_burn`/`swap_and_burn` in this final pass (tool access ended before that read completed), so the exact numeric conditions under which `ether_gained` truncates to zero versus merely "very small" are not fully confirmed from source — this should be verified directly against `swap_exact_tokens_for_tokens`'s rounding behavior and any pool-minimum enforcement before treating the exact zero-fee case as proven; the absence of any minimum-fee `ensure!` on this specific call path, however, is directly confirmed from the code shown above.

### Citations

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L358-364)
```rust

			// Basic validation of new costs. Particularly for token registration, we want to ensure
			// its relatively expensive to discourage spamming. Like at least 100 USD.
			ensure!(
				create_asset_xcm > 0 && transfer_asset_xcm > 0 && register_token > meth(100),
				Error::<T>::InvalidTokenTransferFees
			);
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L210-252)
```rust
		/// Initiates the registration for a Polkadot-native token as a wrapped ERC20 token on
		/// Ethereum.
		/// - `asset_id`: Location of the asset
		/// - `metadata`: Metadata to include in the instantiated ERC20 contract on Ethereum
		///
		/// All origins are allowed, however `asset_id` must be a location nested within the origin
		/// consensus system.
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
```
