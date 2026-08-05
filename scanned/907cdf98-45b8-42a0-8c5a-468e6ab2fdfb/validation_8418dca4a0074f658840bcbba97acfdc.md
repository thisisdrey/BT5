Found a concrete analog: `swap_and_burn` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` explicitly passes `None` for the minimum-output-amount parameter when calling `T::Swap::swap_exact_tokens_for_tokens`, wiping out the slippage protection that `pallet_asset_conversion` otherwise provides everywhere else in the codebase.

### Title
Snowbridge `swap_and_burn` disables AMM slippage protection when swapping tip/fee assets for Ether - (File: bridges/snowbridge/pallets/system-frontend/src/lib.rs)

### Summary
`pallet_asset_conversion` is designed so that every swap path — the public extrinsics, the `Swap`/`SwapCredit` traits, `MutateLiquidity`, the tx-payment adapter, the XCM `SingleAssetExchangeAdapter`, and even the `pallet-revive` precompile — always forwards a caller-chosen `amount_out_min`/`amount_in_max` to protect against adverse price movement [1](#0-0) . The one exception is Snowbridge's `system-frontend` pallet: when swapping a user's tip or registration fee asset into Ether prior to burning it for teleport, it calls `swap_exact_tokens_for_tokens` with the minimum-output argument hardcoded to `None`.

### Finding Description
In `Pallet::<T>::swap_and_burn`, the call to the injected `T::Swap` (a `pallet_asset_conversion::Swap` implementation) is: [2](#0-1) 

The `None` here means `do_swap_exact_tokens_for_tokens` skips the `ProvidedMinimumNotSufficientForSwap` check entirely and accepts whatever `amount_out` the pool state yields at execution time [3](#0-2) . This is the direct analog of the Multipool.sol bug: `rebalanceAll` withdrew/deposited liquidity without `amount0Min`/`amount1Min` even though the underlying `deposit`/`withdraw` primitives supported such protection — here, `swap_and_burn` invokes a primitive that supports `amount_out_min` but deliberately discards it.

The corrupted value is `ether_gained` (the AMM output), which is fed straight into `burn_for_teleport` and then embedded as the `amount` field of the `RegisterToken`/`AddTip` XCM `Transact` call sent to BridgeHub [4](#0-3) [5](#0-4) . No existing guard downstream re-validates that `ether_gained` corresponds to a fair price — `do_swap_exact_tokens_for_tokens` only checks `amount_out >= amount_out_min` when `amount_out_min` is `Some`, and here it is always `None` [6](#0-5) .

### Impact Explanation
This is a public, permissionless entrypoint (`add_tip` is signed by any account; `register_token` is reachable by any origin owning the asset) that performs an AMM swap of user funds with no floor on the output. An attacker can manipulate the tip-asset/Ether pool reserve ratio immediately before/around the extrinsic's execution (e.g., via same-block ordering/MEV against a thin pool, which Snowbridge's own tip/fee pools are likely to be given they're niche asset pairs) so that `swap_exact_tokens_for_tokens` executes at a heavily unfavorable price. Because the resulting `ether_gained` becomes both the burned amount and the tip/registration amount reported to BridgeHub, the caller systematically loses value on every call, and the emergent relayer incentive (`AddTip`) and Ethereum-side registration fee are computed from a manipulable, unprotected quantity — a direct value-loss/mis-priced settlement bug feeding into the bridge's outbound message accounting.

### Likelihood Explanation
The path requires no privileged actor, admin, governance, relayer, or validator collusion — any signed account calling `add_tip` (or any origin calling `register_token`) exercises the vulnerable swap. Likelihood is bounded by how liquid/thin the configured tip-asset/Ether pool is in a given deployment, but the code itself provides zero protection regardless of pool depth, unlike every other swap call site in the repository.

### Recommendation
Thread a caller-supplied (or conservatively pallet-computed, e.g., via `QuotePrice` at call time with a tolerance) `amount_out_min` through `add_tip`/`register_token` into `swap_and_burn`, replacing the hardcoded `None`, mirroring the protection pattern used everywhere else `Swap`/`SwapCredit` is invoked in the codebase (e.g., `SwapAssetAdapter`, `SingleAssetExchangeAdapter`, `SwapFirstAssetTrader`).

### Proof of Concept
1. Deploy/observe a runtime with `snowbridge-pallet-system-frontend` configured with a `T::Swap` backed by `pallet_asset_conversion`, and a tip-asset/Ether pool with modest liquidity.
2. Attacker (or attacker-controlled MEV/ordering) manipulates the pool reserves right before a victim's `add_tip(message_id, asset)` extrinsic executes (e.g., by executing a large swap in the same block ahead of the victim's transaction, then reversing it after).
3. Victim's `add_tip` internally calls `swap_and_burn`, which calls `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)` [2](#0-1) ; `do_swap_exact_tokens_for_tokens` computes `amount_out` from the manipulated reserves and, since `amount_out_min` is `None`, never rejects the unfavorable result [6](#0-5) .
4. The resulting reduced `ether_gained` is burned and reported on-chain as the tip amount via `build_add_tip_call`, permanently understating the victim's intended tip/fee with no recourse, while the attacker profits from the reserve manipulation.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L968-1002)
```rust
		/// Swap exactly `amount_in` of asset `path[0]` for asset `path[1]`.
		/// If an `amount_out_min` is specified, it will return an error if it is unable to acquire
		/// the amount desired.
		///
		/// Withdraws the `path[0]` asset from `sender`, deposits the `path[1]` asset to `send_to`,
		/// respecting `keep_alive`.
		///
		/// If successful, returns the amount of `path[1]` acquired for the `amount_in`.
		///
		/// WARNING: This may return an error after a partial storage mutation. It should be used
		/// only inside a transactional storage context and an Err result must imply a storage
		/// rollback.
		pub(crate) fn do_swap_exact_tokens_for_tokens(
			sender: T::AccountId,
			path: Vec<T::AssetKind>,
			amount_in: T::Balance,
			amount_out_min: Option<T::Balance>,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> Result<T::Balance, DispatchError> {
			ensure!(amount_in > Zero::zero(), Error::<T>::ZeroAmount);
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L237-252)
```rust
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L261-273)
```rust
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L301-308)
```rust
			let ether_gained = T::Swap::swap_exact_tokens_for_tokens(
				who.clone(),
				swap_path,
				tip_amount,
				None, // No minimum amount required
				who,
				true,
			)?;
```
