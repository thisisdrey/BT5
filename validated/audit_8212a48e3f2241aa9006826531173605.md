## Finding

The local analog to the SophonFarming `depositEth()`/`depositWeth()` bug — a public deposit/exchange function that trusts an unvalidated conversion output — exists in `bridges/snowbridge/pallets/system-frontend/src/lib.rs`, in the `swap_and_burn` helper used by the public extrinsics `register_token` and `add_tip`.

### Title
Unbounded-slippage swap lets `add_tip`/`register_token` record and burn a mismatched Ether amount - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

### Summary
`Pallet::<T>::add_tip` and `Pallet::<T>::register_token` are unprivileged, signed extrinsics that accept an arbitrary user-supplied fee asset and forward it to `swap_fee_asset_and_burn` → `swap_and_burn`, which swaps the asset for Ether via `T::Swap::swap_exact_tokens_for_tokens` with the minimum-out parameter hardcoded to `None` ("No minimum amount required").

### Finding Description [1](#0-0) 

The resulting `ether_gained` — whatever amount the AMM happened to return, with zero floor — is used directly as:
1. The burned teleport asset amount (`burn_for_teleport`).
2. The `amount` field forwarded via XCM `Transact` to `EthereumSystemCall::AddTip { amount, .. }` or `EthereumSystemCall::RegisterToken { amount, .. }` on BridgeHub.

On BridgeHub, that `amount` is trusted verbatim and used to increase the relayer reward tip (`Tips` storage in `inbound-queue-v2`, or the outbound queue equivalent) via `AddTip::add_tip`: [2](#0-1) 

and the same "amount" flows into `snowbridge-pallet-system-v2::add_tip`, which just calls into `InboundQueue`/`OutboundQueue::add_tip` and emits `TipProcessed`: [3](#0-2) 

Because the swap has no minimum-output guard, the value that actually gets burned and gets recorded as the relayer's incentive is whatever the pool state (e.g. `pallet_asset_conversion`) yields at execution time — there is no check that it corresponds to a reasonable exchange rate for the fee asset supplied. This exactly mirrors the SophonFarming pattern: a public entrypoint assumes a conversion happens correctly and forwards the raw result downstream without validating it against the user's intended value.

### Impact Explanation
A caller (or the AMM pool being thin/imbalanced at call time, including via normal usage, not requiring a malicious peer/validator) can end up burning `fee_amount` of an arbitrary asset while the derived `ether_gained` — and thus the on-chain relayer tip / registration deposit amount recorded on BridgeHub — is disproportionately small. This causes:
- Loss of user funds (asset burned for near-zero recorded tip/deposit value), matching "permanent user-fund... lock" / theft-adjacent value loss.
- Underpriced relayer incentives that can stall bridge message processing, matching "public underpriced work that degrades block production or stalls bridge processing."

No governance/admin/relayer/validator misbehavior is required — this is triggerable by any signed account calling `add_tip` or `register_token` with any Swap-config'd pool, since the code path itself performs no slippage validation.

### Likelihood Explanation
High: `None` is hardcoded as the minimum-amount parameter, so the vulnerability is always present regardless of pool depth; it only requires an AMM pool with the fee asset/Ether pair and any signed caller, both of which are part of the pallet's normal expected usage per `bridges/snowbridge/docs` and the integration tests (`add_tip_from_asset_hub_user_origin`).

### Recommendation
Add an explicit minimum-out parameter (or on-chain slippage bound derived from a quoted price / max allowed deviation) to `swap_and_burn`'s call to `T::Swap::swap_exact_tokens_for_tokens`, and fail the extrinsic (reverting the whole tip/registration flow) if the achieved `ether_gained` falls below that bound, instead of silently accepting whatever the AMM returns.

### Proof of Concept
1. Configure/observe a `pallet_asset_conversion` pool for `(tip_asset_location, ether_location)` with low liquidity or a skewed ratio.
2. Call `EthereumSystemFrontend::add_tip(origin, message_id, Asset { id: tip_asset_location, fun: Fungible(large_amount) })`.
3. `swap_and_burn` executes `T::Swap::swap_exact_tokens_for_tokens(..., None, ...)`, returning an arbitrarily small `ether_gained` for the given large input, per current pool pricing — no revert occurs.
4. `burn_for_teleport` burns only `ether_gained` (small) while the user's full `large_amount` tip asset was consumed by the swap.
5. `TipProcessed`/`Tips` storage on BridgeHub records only the small `ether_gained` as the reward, showing that most of the user's value was lost with no corresponding increase in relayer incentive or registered amount. [4](#0-3)

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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-258)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			// If the nonce is already processed, return an error
			ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed);
			// Otherwise add the tip.
			Tips::<T>::mutate(nonce, |tip| {
				*tip = Some(tip.unwrap_or_default().saturating_add(amount));
			});
			return Ok(());
		}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L251-281)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::add_tip())]
		pub fn add_tip(
			origin: OriginFor<T>,
			sender: AccountIdOf<T>,
			message_id: MessageId,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let result = match message_id {
				Inbound(nonce) => <T as pallet::Config>::InboundQueue::add_tip(nonce, amount),
				Outbound(nonce) => <T as pallet::Config>::OutboundQueue::add_tip(nonce, amount),
			};

			if let Err(ref e) = result {
				tracing::debug!(target: LOG_TARGET, ?e, ?message_id, ?amount, "error adding tip");
				LostTips::<T>::mutate(&sender, |lost_tip| {
					*lost_tip = lost_tip.saturating_add(amount);
				});
			}

			Self::deposit_event(Event::<T>::TipProcessed {
				sender,
				message_id,
				amount,
				success: result.is_ok(),
			});

			Ok(())
		}
```
