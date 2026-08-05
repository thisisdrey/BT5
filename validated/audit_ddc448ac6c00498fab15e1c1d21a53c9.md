Audit Report

## Title
Missing slippage protection (`amount_out_min = None`) in `swap_and_burn` allows spot-price manipulation of the AssetConversion pool to inflate/deflate Snowbridge relayer reward/tip credit - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`)

## Summary
`Pallet::swap_and_burn` calls `T::Swap::swap_exact_tokens_for_tokens` with the minimum-output parameter hard-coded to `None`, so the tip/registration-fee swap executes at whatever spot price the `pallet-asset-conversion` pool has at that instant, with no bound. The resulting `ether_gained` is burned via `burn_for_teleport` and forwarded verbatim as the `amount` field in the `AddTip`/`RegisterToken` XCM `Transact` call to `EthereumSystem` on BridgeHub, where `system-v2::add_tip` and `register_token` credit that number directly to `InboundQueue`/`OutboundQueue::add_tip(nonce, amount)` or as the registration fee, with no independent re-validation of the amount against real value contributed.

## Finding Description
`add_tip` (call index 2, `ensure_signed`) and `register_token` (call index 1, any origin satisfying `RegisterTokenOrigin`) both call `swap_fee_asset_and_burn`, which for any non-ether fee asset calls `swap_and_burn`: [1](#0-0) [2](#0-1) 

The swap itself is unprotected: [3](#0-2) 

`ether_gained` is then forwarded downstream unchanged, first via XCM `Transact` to BridgeHub's `EthereumSystemCall::AddTip`/`RegisterToken`: [4](#0-3) 

On BridgeHub, `snowbridge-pallet-system-v2::add_tip` takes this `amount` at face value and credits it directly to the inbound/outbound reward queue without recomputing or bounding it: [5](#0-4) 
`register_token` similarly forwards `amount` as the message `fee` with no re-check: [6](#0-5) 

`burn_for_teleport` only withdraws/burns exactly the `ether_gained` amount from the caller's own account — it does not validate that the amount is "fair" relative to any reference price: [7](#0-6) 

Since `add_tip` is a public, unprivileged, signed extrinsic and the swap executes against live AMM reserves with no `amount_out_min` and no TWAP/delta check, an attacker who also controls (or can transiently skew, e.g. via a preceding swap in the same block/`batch_all`) the `fee_asset↔ether` pool's reserves can cause `ether_gained` to reflect a manipulated spot price rather than a fair value, and that manipulated number becomes the authoritative reward-credit amount recorded on BridgeHub/Ethereum.

## Impact Explanation
This fits the "public underpriced work / duplicate settlement / wrong beneficiary or amount" class described in the impact gate: an unprivileged caller can cause the bridge's relayer-reward accounting (`TipProcessed` amount, `RegisterToken` fee) to be settled at an artificially inflated or deflated exchange rate rather than a value tied to actual economic contribution, because the sole gate — `pallet-asset-conversion`'s spot price — is manipulable within a single atomic transaction and the code explicitly disables the standard `amount_out_min` protection (`None`). The exact corrupted value is `ether_gained` in `swap_and_burn`, which propagates unchecked into `EthereumSystemCall::AddTip.amount` / `RegisterToken.amount` and then into the reward ledger via `InboundQueue`/`OutboundQueue::add_tip`.

## Likelihood Explanation
The attack requires only ordinary, unprivileged capabilities: a signed account with capital to move a `pallet-asset-conversion` pool's reserves and standard tooling (`pallet_utility::batch_all`) to sequence a manipulating swap immediately before/after `add_tip`, entirely within one block/transaction. No governance, validator, or off-chain relayer compromise is needed, and the vulnerable code path (`None` passed for slippage) is unconditional — it exists on every call to `swap_and_burn`, making the issue trivially repeatable subject to available liquidity/capital for pool manipulation and swap fees incurred in reversing the price.

## Recommendation
`swap_and_burn` must not hard-code `None` for the minimum-output parameter when the swap output feeds bridge reward/fee accounting. Use `pallet_asset_conversion::Pallet::quote_price_exact_tokens_for_tokens` (or an equivalent oracle/reference price) to compute a bounded minimum acceptable `ether_gained`, and pass that as `amount_out_min`, causing the swap (and thus the extrinsic) to fail if the realized price deviates beyond an acceptable tolerance from the reference price — consistent with how other AMM-facing extrinsics require callers to supply `amount_out_min`.

## Proof of Concept
1. Deploy/identify a `pallet-asset-conversion` pool for `(fee_asset, ether_location)` with limited liquidity relative to attacker capital.
2. Build a single `pallet_utility::batch_all` extrinsic containing: (a) a large swap that skews the pool's `fee_asset→ether` reserves to inflate the spot rate, and (b) `snowbridge_pallet_system_frontend::add_tip(message_id, fee_asset_amount)`.
3. Observe that `swap_and_burn`'s internal `swap_exact_tokens_for_tokens` call executes against the skewed reserves with `amount_out_min = None`, producing an `ether_gained` far above the pre-manipulation quote.
4. Confirm `ether_gained` is burned and forwarded as `EthereumSystemCall::AddTip.amount`, and that `system-v2::add_tip` credits this inflated figure directly to `InboundQueue`/`OutboundQueue::add_tip` with no independent bound — verifiable via a unit test asserting `TipProcessed.amount` reflects the manipulated (not fair-market) value.
5. Optionally reverse the initial swap leg in the same batch to restore pool reserves, demonstrating the reward-ledger corruption can be achieved at low net cost to the attacker (bounded mainly by AMM swap fees).

### Citations

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L56-68)
```rust
/// Call indices for dispatchables within `snowbridge-pallet-system-v2`
#[derive(Encode, Decode, Debug, PartialEq, Clone, TypeInfo)]
pub enum EthereumSystemCall<T: frame_system::Config> {
	#[codec(index = 2)]
	RegisterToken {
		sender: Box<VersionedLocation>,
		asset_id: Box<VersionedLocation>,
		metadata: AssetMetadata,
		amount: u128,
	},
	#[codec(index = 3)]
	AddTip { sender: AccountIdOf<T>, message_id: MessageId, amount: u128 },
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

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L211-249)
```rust
		pub fn register_token(
			origin: OriginFor<T>,
			sender: Box<VersionedLocation>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

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

			let command = Command::RegisterForeignToken {
				token_id,
				name: metadata.name.into_inner(),
				symbol: metadata.symbol.into_inner(),
				decimals: metadata.decimals,
			};

			let message_origin = Self::location_to_message_origin(sender_location)?;
			Self::send(message_origin, command, amount)?;

			Self::deposit_event(Event::<T>::RegisterToken {
				location: location.into(),
				foreign_token_id: token_id,
			});

			Ok(())
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

**File:** bridges/snowbridge/primitives/core/src/lib.rs (L192-200)
```rust
pub fn burn_for_teleport<AssetTransactor>(origin: &Location, fee: &Asset) -> XcmResult
where
	AssetTransactor: TransactAsset,
{
	let dummy_context = XcmContext { origin: None, message_id: Default::default(), topic: None };
	AssetTransactor::can_check_out(origin, fee, &dummy_context)?;
	AssetTransactor::check_out(origin, fee, &dummy_context);
	AssetTransactor::withdraw_asset(fee, origin, None)?;
	Ok(())
```
