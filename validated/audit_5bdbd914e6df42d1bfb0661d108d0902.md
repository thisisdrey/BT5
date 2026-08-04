Based on the investigation, I found a concrete local analog in the Snowbridge system-frontend/system-v2/inbound-queue-v2 tip flow, where the core broken invariant matches the report: **an asset is irreversibly deducted/burned on one side of a state transition before the corresponding state update on the other side is confirmed to succeed, and there is no path to reconcile the two once they diverge.**

### Title
Snowbridge relayer tip is burned on Asset Hub even when the Bridge Hub reward can never be credited, permanently destroying user funds with no recovery path - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`, `bridges/snowbridge/pallets/system-v2/src/lib.rs`, `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
`SystemFrontend::add_tip` swaps the caller's asset for Ether and immediately burns it via `burn_for_teleport` before any confirmation that the tip will actually be credited to a relayer reward on Bridge Hub. The credit only happens later, asynchronously, via an XCM `Transact` call into `pallet-system-v2::add_tip`, which forwards to `InboundQueueV2::add_tip`/`OutboundQueueV2::add_tip`. If the target nonce/order has already been consumed by the time this arrives, the call returns `AddTipError`, and the only consequence is that the amount is recorded in `LostTips` — a storage map explicitly documented as existing only to "support implementing a recovery method in the future," i.e. no such recovery currently exists.

### Finding Description
The flow is:
1. User calls `SystemFrontend::add_tip(origin, message_id, asset)` on Asset Hub (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:261-273`).
2. `swap_fee_asset_and_burn` swaps the asset for Ether and calls `burn_for_teleport`, permanently removing the funds from the user's account on this chain [1](#0-0) .
3. An XCM `Transact` is dispatched to Bridge Hub carrying `EthereumSystemCall::AddTip { sender, message_id, amount }` [2](#0-1) .
4. On Bridge Hub, `pallet-system-v2::add_tip` dispatches to `InboundQueue::add_tip` or `OutboundQueue::add_tip` depending on the message id [3](#0-2) .
5. `InboundQueueV2::add_tip` rejects the tip with `AddTipError::NonceConsumed` if `Nonce::<T>::get(nonce)` is already true [4](#0-3) ; `OutboundQueueV2::add_tip` similarly fails with `AddTipError::UnknownMessage` once `PendingOrders` for that nonce has been removed after delivery [5](#0-4) .
6. On failure, `system-v2::add_tip` only records the lost amount into `LostTips::<T>::mutate(&sender, ...)` and still returns `Ok(())` — there is no error propagated back across the bridge, and critically no extrinsic anywhere in the indexed code that lets a user withdraw or reclaim their `LostTips` balance [6](#0-5) [7](#0-6) .

This mirrors the H-9 pattern exactly: a first action (`reserve` / here, `burn_for_teleport`) commits an irreversible state change (locker no longer sellable / funds burned) *before* the dependent secondary state (protected listing / reward credit) is guaranteed to be established and stay established. When the secondary state is later invalidated (protected listing removed without withdraw / target nonce already consumed by the time the XCM arrives), the value that was already committed is orphaned — in the Flayer case leading to a double sale, here leading to permanently destroyed user funds with only a bookkeeping stub (`LostTips`) and no reconciliation path.

An unprivileged user can trigger this without any admin, relayer, or validator misbehavior — simply by calling `add_tip` for a message whose nonce is concurrently (and legitimately) being processed by a relayer. Given the asynchronous, non-atomic nature of the XCM transact call between chains, this is a race condition inherent to the design, not an edge case requiring malicious actors.

### Impact Explanation
This is real, permanent user-fund loss caused by ordinary usage (not by any privileged/malicious actor), fitting squarely within the “permanent user-fund or bridge-state lock” and “theft or unbacked … loss” impact categories for the HackenProof/Snowbridge scope. The user's asset is burned via `burn_for_teleport` unconditionally at step 2, regardless of whether the credit at step 5 ever succeeds.

### Likelihood Explanation
Likelihood is moderate-to-high: any tip submitted for a message nonce that is concurrently processed by a relayer (a normal, expected, frequent race in a live bridge) will trigger this. No adversarial coordination is required — it is simply a timing race between the tip-add path and the relayer message-processing path, both of which are ordinary, permissionless operations.

### Recommendation
Do not burn/withdraw the tip asset on Asset Hub until Bridge Hub confirms (via a callback/receipt or two-phase commit) that the tip was successfully applied to a live nonce/order. Alternatively, hold the swapped Ether in an escrow account on Asset Hub (rather than burning for teleport) until confirmation, and add a signed extrinsic allowing users to reclaim their `LostTips` balance, since currently there is no such recovery mechanism.

### Proof of Concept
1. Relayer submits `InboundQueueV2::submit` for nonce `N`; this sets `Nonce::<T>::set(N)` at the start of `process_message` (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:219-225`).
2. Concurrently (same block or shortly after, before finality of step 1 is known to the user), the user calls `SystemFrontend::add_tip` targeting nonce `N`, which immediately burns their fee asset via `swap_fee_asset_and_burn` (`bridges/snowbridge/pallets/system-frontend/src/lib.rs:267`).
3. The XCM `Transact` carrying `AddTip{ message_id: Inbound(N), amount }` arrives at Bridge Hub after `process_message` has already consumed nonce `N`.
4. `InboundQueueV2::add_tip` returns `Err(AddTipError::NonceConsumed)` (`bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs:252`).
5. `system-v2::add_tip` catches the error and stores it in `LostTips`, but the extrinsic still returns `Ok(())` and no funds are ever returned to the user (`bridges/snowbridge/pallets/system-v2/src/lib.rs:266-271`).

Note: I was not able to fully verify whether any downstream recovery mechanism for `LostTips` exists outside the indexed portion of the codebase (the pallet's own comment states this is a documented gap: "Capturing the lost tips here supports implementing a recovery method **in the future**"). If a recovery extrinsic exists elsewhere and simply wasn't surfaced by search, this reduces the impact to a UX/latency issue rather than permanent loss — that should be confirmed with a full repository search (e.g., via a Devin session) before treating this as fully proven.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-496)
```rust
		/// Process a delivery receipt from a relayer, to allocate the relayer reward.
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
	}

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
