Audit Report

## Title
Permanent loss of relayer-tip funds with no recovery path when tip attachment races message settlement - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

## Summary
`pallet_system_v2::add_tip` forwards a tip to `InboundQueue::add_tip` / `OutboundQueue::add_tip`, but the corresponding user-facing `pallet_system_frontend::add_tip` on AssetHub already irrevocably swaps and burns the user's tip asset via `swap_fee_asset_and_burn` / `burn_for_teleport` *before* the cross-chain `Transact` call reaches BridgeHub. If the underlying nonce/order has already been settled by a relayer by the time the tip call executes on BridgeHub, the tip cannot be attached and is only recorded in `LostTips`, with no extrinsic anywhere in the pallet to reclaim it — the already-burned funds are permanently lost.

## Finding Description
On AssetHub, `pallet_system_frontend::add_tip` burns the tip asset unconditionally and synchronously before ever knowing whether the target message is still pending: [1](#0-0) [2](#0-1) 

This burn happens via `burn_for_teleport`/`swap_and_burn` unconditionally, then an `UnpaidExecution` XCM `Transact` is fired to BridgeHub with no rollback mechanism: [3](#0-2) 

On BridgeHub, `pallet_system_v2::add_tip` attempts to attach the tip to the reward via the queue pallets. If the nonce (inbound) or order (outbound) has already been settled — which happens independently and asynchronously via `process_message` / `process_delivery_receipt` when a relayer submits proof — the attach fails and the amount is only recorded as a bookkeeping placeholder in `LostTips`, with the call itself still returning `Ok(())`: [4](#0-3) [5](#0-4) 

The queue-side guards that cause the failure are the nonce check and the `PendingOrders` lookup: [6](#0-5) [7](#0-6) 

The `LostTips` storage doc comment itself confirms no recovery mechanism currently exists — only "supports implementing a recovery method in the future." The `#[pallet::call]` block of `pallet_system_v2` contains only `upgrade`, `set_operating_mode`, `register_token`, and `add_tip` — no claim/refund call exists on either BridgeHub or AssetHub.

The exploit path requires no privileged actor: a relayer promptly submitting `process_message` (inbound) or `process_delivery_receipt` (outbound) for a nonce — which is normal, expected relayer behavior — before a user's already-dispatched `add_tip` XCM lands on BridgeHub, is sufficient to strand the burned funds forever with no compensating mint, refund, or claim path.

## Impact Explanation
This matches the "permanent user-fund or bridge-state lock" impact category. The corrupted/lost value is the tip amount recorded in `LostTips::<T>` on BridgeHub, while the actual backing asset was already destroyed on AssetHub via `burn_for_teleport` — a genuine one-way, irreversible loss of real user funds triggered by an ordinary and expected asynchronous-processing race rather than by attacker-controlled malicious behavior.

## Likelihood Explanation
High. Both the AssetHub-side burn and the BridgeHub-side settlement (via nonce consumption or delivery receipt processing) are normal-path, publicly-triggerable operations reachable by any unprivileged relayer/user pair. No collusion, privileged origin, or leaked keys are needed — only ordinary timing where message settlement outpaces tip delivery, which is entirely plausible given they travel via independent extrinsics/XCM hops.

## Recommendation
- Add a `claim_lost_tip` extrinsic (or an XCM-triggerable equivalent reachable from AssetHub) allowing the recorded sender to reclaim/re-mint their `LostTips::<T>` balance.
- Alternatively, restructure the flow so the AssetHub-side burn is deferred until BridgeHub confirms the nonce/order is still pending (e.g., synchronous confirmation before burning, or hold-and-release semantics), eliminating the possibility of destroying funds for tips that can never be attached.

## Proof of Concept
1. User calls `pallet_system_frontend::add_tip` on AssetHub for a pending inbound/outbound message; the tip asset is swapped and burned immediately (`swap_fee_asset_and_burn`), and an XCM `Transact` carrying `EthereumSystemCall::AddTip` is sent to BridgeHub.
2. Before that XCM executes on BridgeHub, a relayer submits the message proof or delivery receipt, causing `Nonce::<T>::set(nonce)` (inbound) or `PendingOrders::<T>::remove(nonce)` (outbound) to run first.
3. The `AddTip` XCM executes `pallet_system_v2::add_tip`, which calls into `InboundQueue::add_tip`/`OutboundQueue::add_tip`, returns `Err(AddTipError::NonceConsumed)`/`Err(AddTipError::UnknownMessage)`, and the amount is written into `LostTips::<T>` with the call still returning `Ok(())`.
4. There is no extrinsic on either AssetHub (`pallet_system_frontend`) or BridgeHub (`pallet_system_v2`) to reclaim `LostTips`, so the user's already-burned funds are permanently unrecoverable.

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
