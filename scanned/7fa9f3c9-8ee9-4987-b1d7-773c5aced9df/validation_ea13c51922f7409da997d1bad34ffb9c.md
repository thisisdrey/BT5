### Title
Missing slippage protection in `snowbridge-pallet-system-frontend`'s tip/fee swap allows sandwich extraction of user funds - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`snowbridge-pallet-system-frontend::Pallet::swap_and_burn` calls `pallet_asset_conversion`'s `Swap::swap_exact_tokens_for_tokens` with `amount_out_min` hardcoded to `None`, explicitly commented "No minimum amount required". This is functionally identical to the Talos/Maia finding: a public, unprivileged code path performs an AMM swap with zero slippage protection, exposing the user's tip/fee asset to full-slippage loss.

### Finding Description
`swap_and_burn` is invoked from two unprivileged, user-facing extrinsics:
- `register_token` (callable by any origin whose location matches `RegisterTokenOrigin`, i.e. any parachain/account registering its own asset) via `swap_fee_asset_and_burn`
- `add_tip` (callable by `ensure_signed`, i.e. any signed account) [1](#0-0) 

```rust
fn swap_and_burn(...) -> Result<u128, DispatchError> {
    let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
        who.clone(),
        swap_path,
        tip_amount,
        None, // No minimum amount required
        who,
        true,
    )?;
    ...
}
``` [2](#0-1) 

`pallet_asset_conversion`'s underlying dispatchable/trait implementation itself supports and enforces a caller-supplied `amount_out_min` (see `do_swap_exact_tokens_for_tokens`, which checks `amount_out >= amount_out_min` when `Some` is provided) [3](#0-2) . The `system-frontend` pallet, however, bypasses this protection entirely by hardcoding `None`, meaning the resulting `ether_gained` (the amount that becomes the register-token deposit or the relayer tip forwarded to BridgeHub) can be arbitrarily depressed by adverse pool price movement between block inclusion and execution, or by intervening trades against the same pool in the mempool/queue, with zero floor.

### Impact Explanation
`ether_gained` directly determines:
1. The `amount` field sent in the `RegisterToken` Transact call to BridgeHub's `EthereumSystem::register_token`, which funds the on-chain registration/execution cost on Ethereum [4](#0-3) .
2. The `amount` field sent in `AddTip`, which is credited as the relayer reward for a specific outbound message [5](#0-4) .

Because the swap has no minimum-output guard, the pool can return a near-zero amount of Ether for the user's deposited tip/fee asset while the pool absorbs the value, i.e. a real, unbacked loss of user funds. Downstream, an artificially low `ether_gained` also means an underfunded/underpriced `register_token` or `add_tip` message is forwarded into the Snowbridge delivery flow — potentially causing tips too small to attract relayers (message stalls) or asset registration paid for less than the actual required execution cost on Ethereum, degrading bridge processing.

### Likelihood Explanation
Both `register_token` and `add_tip` are unprivileged, permissionless entry points reachable by any account holding a fee/tip asset and access to a shallow-liquidity AssetConversion pool (a completely permissionless AMM anyone can create/manipulate via `pallet_asset_conversion::add_liquidity`/`remove_liquidity`/`swap_exact_tokens_for_tokens`). No malicious validator, collator, relayer, or governance actor is required — only an ordinary account executing swaps against the same pool before/around the victim's transaction, or natural pool illiquidity/price movement. This exactly mirrors the accepted root cause of the referenced Talos finding: a public entry point performing an AMM interaction with `amountOutMin`/`amount_out_min` unconditionally set to the no-protection value.

### Recommendation
Add a caller-supplied (or configurably enforced) minimum-output parameter to `register_token` and `add_tip`, and thread it through to `swap_and_burn`/`T::Swap::swap_exact_tokens_for_tokens` instead of hardcoding `None`. At minimum, derive a safety bound (e.g., quote via `QuotePrice` immediately before the swap and require `ether_gained` to be within an acceptable tolerance of the quoted amount), consistent with how `pallet-asset-conversion-tx-payment::SwapAssetAdapter` uses an exact quoted amount with `swap_tokens_for_exact_tokens` and asserts zero change rather than accepting an unbounded output.

### Proof of Concept
1. A liquidity pool exists for `(tip_asset, Ether)` in `pallet_asset_conversion` with modest reserves.
2. Attacker (any permissionless account) submits a large `swap_exact_tokens_for_tokens` trade against the same pool immediately before the victim's `add_tip` (or `register_token`) transaction, or simply front-runs by adding/removing liquidity, temporarily distorting the price.
3. Victim's `add_tip(message_id, asset)` executes `swap_and_burn`, which calls `T::Swap::swap_exact_tokens_for_tokens(..., tip_amount, None, ...)` — with no floor, the swap succeeds even though `ether_gained` is far below the fair-market amount.
4. Attacker reverses their trade (or simply captured the spread), while the victim's tip asset is consumed and only a fraction of the expected Ether-denominated tip/fee is credited and forwarded to BridgeHub in the `AddTip`/`RegisterToken` Transact call.
5. Result: user funds lost to the pool/attacker with no protection, and BridgeHub receives an artificially small tip/fee amount, potentially insufficient to incentivize relaying or to fund cross-chain execution.

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L319-338)
```rust
		// Build the call to dispatch the `EthereumSystem::register_token` extrinsic on BH
		fn build_register_token_call(
			sender: Location,
			asset: Location,
			metadata: AssetMetadata,
			amount: u128,
		) -> Result<BridgeHubRuntime<T>, Error<T>> {
			// reanchor locations relative to BH
			let sender = Self::reanchored(sender)?;
			let asset = Self::reanchored(asset)?;

			let call = BridgeHubRuntime::EthereumSystem(EthereumSystemCall::RegisterToken {
				sender: Box::new(VersionedLocation::from(sender)),
				asset_id: Box::new(VersionedLocation::from(asset)),
				metadata,
				amount,
			});

			Ok(call)
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L340-351)
```rust
		// Build the call to dispatch the `EthereumSystem::add_tip` extrinsic on BH
		fn build_add_tip_call(
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> BridgeHubRuntime<T> {
			BridgeHubRuntime::EthereumSystem(EthereumSystemCall::AddTip {
				sender,
				message_id,
				amount,
			})
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L989-1002)
```rust
			if let Some(amount_out_min) = amount_out_min {
				ensure!(amount_out_min > Zero::zero(), Error::<T>::ZeroAmount);
			}

			Self::validate_swap_path(&path)?;
			let path = Self::balance_path_from_amount_in(amount_in, path)?;

			let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
			if let Some(amount_out_min) = amount_out_min {
				ensure!(
					amount_out >= amount_out_min,
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
			}
```
