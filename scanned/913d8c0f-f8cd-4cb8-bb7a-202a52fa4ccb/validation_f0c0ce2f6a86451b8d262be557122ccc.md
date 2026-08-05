Confirmed: on AssetHub, `add_tip` in `system-frontend` swaps the user's tip asset for Ether and **burns it via `burn_for_teleport`** before the XCM `Transact` even reaches BridgeHub. The XCM is sent `UnpaidExecution`/best-effort — there is no guarantee of delivery or successful execution on BridgeHub. On BridgeHub, `EthereumSystemV2::add_tip` (`bridges/snowbridge/pallets/system-v2/src/lib.rs:253-281`) calls `InboundQueue::add_tip`/`OutboundQueue::add_tip`, and if that inner call errors (e.g., stale/unknown/already-consumed nonce), the error is **swallowed**: the funds are already burned on AssetHub, the extrinsic on BridgeHub still returns `Ok(())`, and the only trace is an entry added to `LostTips` — a storage map with no implemented recovery/claim mechanism (the doc comment explicitly says it "supports implementing a recovery method in the future", i.e., not implemented now).

### Title
Silent tip-loss on stale/invalid nonce in `snowbridge-pallet-system-v2::add_tip` — burned funds permanently unrecoverable - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
`EthereumSystemV2::add_tip` never propagates the inner `AddTip::add_tip` error to the caller; on failure it just records the amount in `LostTips` and returns `Ok(())`. Because the corresponding Ether was already swapped and irreversibly burned on AssetHub in `snowbridge_pallet_system_frontend::add_tip` *before* the XCM `Transact` is even dispatched, any timing/race condition or user mistake in nonce selection causes permanent, unrecoverable loss of the tip, mirroring the affiliate-fee "silently skip on bad input instead of reverting" defect from the external report. [1](#0-0) [2](#0-1) 

### Finding Description
The flow is:
1. User calls `system_frontend::add_tip(message_id, asset)` on AssetHub. `swap_fee_asset_and_burn` swaps the tip asset for Ether and calls `burn_for_teleport`, irreversibly destroying the local asset representation of the value. [3](#0-2) 
2. A best-effort, unpaid `Transact` XCM carrying `EthereumSystemCall::AddTip{ sender, message_id, amount }` is sent to BridgeHub. [4](#0-3) 
3. On BridgeHub, `EthereumSystemV2::add_tip` dispatches to `InboundQueue::add_tip(nonce, amount)` (writes to the `Tips` map keyed by nonce, only valid if the message with that nonce has not yet been processed — see `Nonce::<T>::get(nonce)` check in `inbound-queue-v2::process_message`) or `OutboundQueue::add_tip(nonce, amount)` (mutates `PendingOrders` fee, only valid while the order still exists). [5](#0-4) [6](#0-5) 
4. If either message has already been processed/delivered (a very plausible race — the user picks a nonce for an inbound Ethereum message that a relayer is concurrently submitting, or an outbound message whose delivery receipt has already been confirmed), `add_tip` returns `Err(AddTipError::UnknownMessage)`.
5. `EthereumSystemV2::add_tip` catches this error, adds the amount to `LostTips::<T>` keyed by sender, emits `TipProcessed{ success: false }`, and **still returns `Ok(())`** — the dispatchable never reverts. [7](#0-6) 

This is functionally identical to the reported bug class: a caller-controlled input (message nonce) that is invalid/stale causes the payout-routing logic to be quietly skipped rather than the whole operation reverting — except here it is worse, because unlike the affiliate case (where the mint itself still completed and no extra value was destroyed), the tip value has **already been irrecoverably burned** on the source chain before the failure is even detected on BridgeHub. There is no atomic linkage between the burn on AssetHub and the accounting on BridgeHub — the burn happens unconditionally, then the credit is attempted only as best effort.

### Impact Explanation
This falls into the "permanent user-fund lock" bucket explicitly named in the Polkadot SDK Pivots: message queues, bridge markers, and payout state should only advance after decode, dispatch, execution, and settlement succeed atomically, but here the source-side value destruction (`burn_for_teleport`) is decoupled from and unconditionally precedes the destination-side crediting attempt. Any unprivileged user who submits `add_tip` for a nonce that races with normal relayer message processing loses their tip funds permanently, with only a `LostTips` bookkeeping entry and no implemented claim/recovery path in this codebase.

### Likelihood Explanation
This requires no malicious actor, governance action, or privileged access — it is triggered purely by normal, unprivileged use of the public `add_tip` extrinsic under ordinary network timing (a relayer processing the targeted message before, or concurrently with, the tip transaction landing on BridgeHub via cross-chain XCM). Given XCM transport delay between AssetHub and BridgeHub, and that Ethereum message processing/relayer submission proceeds independently and asynchronously, this race is realistically probable, not a contrived edge case.

### Recommendation
Do not burn/destroy the tip asset on AssetHub until the corresponding tip has been durably accepted on BridgeHub. Options:
- Reverse the order: reserve/hold rather than burn on AssetHub until an acknowledgment of success is received back from BridgeHub, only finalizing the burn on confirmed success, and refunding on failure.
- Alternatively, if a two-phase/asynchronous flow is unavoidable, implement the promised recovery method for `LostTips` now (a signed extrinsic allowing the original sender to reclaim/redirect their `LostTips` balance) rather than leaving it as a documented "future" gap.
- At minimum, make the nonce-validity window check happen before the burn (e.g. by having AssetHub query or precondition on the nonce's pending status before initiating the burn), reducing — though not fully eliminating — the race window.

### Proof of Concept
1. On BridgeHub, a relayer submits `EthereumInboundQueueV2::submit` for message with `nonce = N`; it is processed and `Nonce::<T>::set(N)` is set, i.e., the message can no longer be tipped.
2. Concurrently (before the user is aware), the user on AssetHub calls `SnowbridgeSystemFrontend::add_tip(MessageId::Inbound(N), tip_asset)`. `swap_fee_asset_and_burn` swaps and burns the user's DOT/asset for Ether — value is destroyed.
3. The resulting XCM `Transact(EthereumSystemCall::AddTip{ sender, message_id: Inbound(N), amount })` arrives at BridgeHub after step 1 has already completed.
4. `InboundQueue::add_tip(N, amount)` finds `Nonce::<T>::get(N) == true` was already handled or the tip map no longer relevant (see `test_add_tip_cumulative`/`add_tip_inbound_fails_when_nonce_is_consumed` test analog), returning `Err(AddTipError::UnknownMessage)`. [8](#0-7) 
5. `EthereumSystemV2::add_tip` swallows the error, records it in `LostTips::<T>::get(sender)`, and returns `Ok(())`. The user's burned funds are gone with no on-chain path to reclaim them.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L253-281)
```rust
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

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L340-363)
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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L214-245)
```rust
	impl<T: Config> Pallet<T> {
		pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);

			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

			let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
				.map_err(|e| match e {
					MessageProcessorError::ProcessMessage(e) => e,
					MessageProcessorError::ConvertMessage(e) => Error::<T>::from(e).into(),
					MessageProcessorError::SendMessage(e) => Error::<T>::from(e).into(),
				})?;

			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}

			// Emit event with the message_id
			Self::deposit_event(Event::MessageReceived { nonce, message_id });

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

**File:** bridges/snowbridge/pallets/system-v2/src/tests.rs (L197-219)
```rust
#[test]
fn add_tip_inbound_fails_when_nonce_is_consumed() {
	new_test_ext(true).execute_with(|| {
		let origin = make_xcm_origin(FrontendLocation::get());
		let sender: AccountId = Keyring::Alice.into();
		// In `MockOkInboundQueue`, the mocked implementation returns an error when the nonce is
		// equal to 3, to simulate an error condition.
		let message_id = MessageId::Inbound(FAILING_NONCE);
		let amount = 1000;

		assert_ok!(EthereumSystemV2::add_tip(origin, sender.clone(), message_id.clone(), amount));

		System::assert_last_event(RuntimeEvent::EthereumSystemV2(Event::<Test>::TipProcessed {
			sender: sender.clone(),
			message_id,
			amount,
			success: false,
		}));

		let lost_tip = LostTips::<Test>::get(sender);
		assert_eq!(lost_tip, 1000);
	});
}
```
