All code details in the claim check out exactly against the repository. The burn happens synchronously in `add_tip` on the frontend pallet via `swap_fee_asset_and_burn` before the XCM `Transact` is even sent, and the crediting on BridgeHub via `pallet_snowbridge_system_v2::add_tip` is a separate async step whose failure only writes to `LostTips` with no reversal mechanism.

Audit Report

## Title
Relayer tip funds are burned on AssetHub before the corresponding reward credit is confirmed on BridgeHub, causing permanent loss of user funds when the tip fails to attach - (File: bridges/snowbridge/pallets/system-frontend/src/lib.rs, bridges/snowbridge/pallets/system-v2/src/lib.rs)

## Summary
`snowbridge_pallet_system_frontend::Pallet::add_tip` unconditionally swaps and burns the user's tip asset on AssetHub via `swap_fee_asset_and_burn`/`burn_for_teleport` before sending an unpaid, best-effort `Transact` XCM to BridgeHub to credit the tip to a pending order's fee. If the targeted `PendingOrders` entry has already been removed by `process_delivery_receipt` (a normal race with relayer activity), `pallet_snowbridge_system_v2::add_tip` only records the amount into `LostTips`, with no code path anywhere in the repository that mints, refunds, or otherwise reissues that value. [1](#0-0) [2](#0-1) 

## Finding Description
`add_tip` in the system-frontend pallet calls `swap_fee_asset_and_burn`, which either swaps the tip asset to Ether and burns it, or, if already Ether, directly calls `burn_for_teleport` — an irreversible operation executed synchronously in the same extrinsic on AssetHub. [3](#0-2)  Only after this burn does the pallet build an `AddTip` call and dispatch it via an `UnpaidExecution` `Transact` XCM toward BridgeHub. [4](#0-3) 

On BridgeHub, `pallet_snowbridge_system_v2::add_tip` forwards the amount to `OutboundQueue::add_tip`/`InboundQueue::add_tip`. The outbound implementation only succeeds if `PendingOrders::<T>` still contains an entry for that nonce; otherwise it returns `AddTipError::UnknownMessage`. [5](#0-4)  `process_delivery_receipt`, which is invoked whenever a relayer submits a delivery receipt for that nonce, removes the `PendingOrders` entry as part of settling the reward. [6](#0-5)  If this happens before the tip's XCM `Transact` executes on BridgeHub, `add_tip` fails and `pallet_snowbridge_system_v2::add_tip` records the lost amount into `LostTips`, still returning `Ok(())` for the extrinsic. [7](#0-6)  There is no code elsewhere in the pallet, nor any related recovery extrinsic, that reads and redeems `LostTips` — the storage comment itself states it only "supports implementing a recovery method in the future," confirming no such mechanism currently exists. The burn on AssetHub is never rolled back once this failure occurs, so the value is permanently destroyed with no corresponding credit anywhere in the system.

## Impact Explanation
This matches the "permanent user-fund lock"/value-conservation impact category: value is irreversibly destroyed on AssetHub without a guaranteed, atomic corresponding credit on BridgeHub, and the fallback bookkeeping (`LostTips`) has no payout path in the current codebase. This is a genuine implementation bug in production Snowbridge V2 bridge/runtime pallet logic reachable by any ordinary signed account, not a privileged or off-repo issue.

## Likelihood Explanation
The race condition requires no special privileges — only ordinary asynchronous cross-chain message timing between a user's `add_tip` extrinsic (relayed via XCM to BridgeHub) and a relayer's `submit_delivery_receipt` for the same nonce. Because users are economically incentivized to tip messages that are close to being relayed (to nudge a near-complete delivery), this race is a natural and repeatable occurrence rather than a contrived edge case.

## Recommendation
Avoid burning the tip asset on AssetHub until BridgeHub confirms acceptance of the tip against a still-pending order (e.g., via reserving the asset and finalizing burn only on confirmation, or using a two-phase commit/ack pattern). Alternatively, implement an actual redemption mechanism for `LostTips` that reliably remints/refunds the exact amount to the exact `sender` recorded, closing the current gap where `LostTips` is purely informational.

## Proof of Concept
1. A message is sent via `pallet_snowbridge_system_v2`, creating a `PendingOrder{nonce, fee}` in `outbound-queue-v2::PendingOrders`. [8](#0-7) 
2. User A calls `system-frontend::add_tip(message_id = Outbound(nonce), asset)` on AssetHub, which immediately burns/swaps-and-burns the tip asset via `swap_fee_asset_and_burn`, then sends an `UnpaidExecution` `Transact` toward BridgeHub. [1](#0-0) 
3. Before that XCM executes on BridgeHub, a relayer submits a delivery receipt for the same nonce, triggering `process_delivery_receipt`, which removes the `PendingOrders` entry. [9](#0-8) 
4. The delayed tip Transact executes; `OutboundQueue::add_tip` returns `AddTipError::UnknownMessage` since the order is gone. [5](#0-4) 
5. `pallet_snowbridge_system_v2::add_tip` records the amount into `LostTips::<T>` for User A and returns `Ok(())`; the burned asset is never restored. [10](#0-9)

### Citations

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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L353-363)
```rust
		fn build_remote_xcm(call: &impl Encode) -> Xcm<()> {
			Xcm(vec![
				DescendOrigin(T::PalletLocation::get()),
				UnpaidExecution { weight_limit: Unlimited, check_origin: None },
				Transact {
					origin_kind: OriginKind::Xcm,
					call: call.encode().into(),
					fallback_max_weight: None,
				},
			])
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

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L136-142)
```rust
	/// Relayer reward tips that were paid by the user to incentivize the processing of their
	/// message, but then could not be added to their message reward (e.g. the nonce was already
	/// processed or their order could not be found). Capturing the lost tips here supports
	/// implementing a recovery method in the future.
	#[pallet::storage]
	pub type LostTips<T: Config> =
		StorageMap<_, Blake2_128Concat, AccountIdOf<T>, u128, ValueQuery>;
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-436)
```rust
			// Generate `PendingOrder` with fee attached in the message, stored
			// into the `PendingOrders` map storage, with assigned nonce as the key.
			// When the message is processed on ethereum side, the relayer will send the nonce
			// back with delivery proof, only after that the order can
			// be resolved and the fee will be rewarded to the relayer.
			let order = PendingOrder {
				nonce,
				fee,
				block_number: frame_system::Pallet::<T>::current_block_number(),
			};
			<PendingOrders<T>>::insert(nonce, order);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-480)
```rust
		pub fn process_delivery_receipt(
			relayer: <T as frame_system::Config>::AccountId,
			receipt: DeliveryReceipt,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);

			let reward_account = if receipt.reward_address == [0u8; 32] {
				relayer
			} else {
				receipt.reward_address.into()
			};

			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}

			<PendingOrders<T>>::remove(nonce);

			Self::deposit_event(Event::MessageDelivered { nonce });

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L483-496)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			PendingOrders::<T>::try_mutate_exists(nonce, |maybe_order| -> Result<(), AddTipError> {
				match maybe_order {
					Some(order) => {
						order.fee = order.fee.saturating_add(amount);
						Ok(())
					},
					None => Err(AddTipError::UnknownMessage),
				}
			})
		}
	}
```
