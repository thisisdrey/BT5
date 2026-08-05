## Analysis

The Jet v2 report's core pattern — value that is legitimately collected from a user but then, due to a state-ordering/accounting gap, becomes stuck or unrecoverable by that user through the protocol's own accounting bookkeeping — has a direct analog in Snowbridge's relayer-tip mechanism. Instead of a rounding error, the polkadot-sdk analog is a **missing settlement path**: funds are debited from a user, recorded as a bookkeeping entry (`LostTips`) when they cannot be applied, and the codebase contains no extrinsic capable of ever returning that value to the user.

### Title
Permanently unclaimable relayer tips recorded in `LostTips` with no recovery mechanism - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
When a user pre-pays a relayer tip for an inbound/outbound Snowbridge message via `pallet_bridge_system_v2::add_tip`, and that tip loses the race against the relayer's `submit`/`process_delivery_receipt` call (i.e., the underlying nonce is already consumed), the paid tip amount is diverted into the `LostTips` storage map instead of the relayer reward. There is no dispatchable, in this codebase, that reads or drains `LostTips`, so the value is permanently stranded on-chain.

### Finding Description
`pallet_bridge_system_v2::add_tip` is the entrypoint reached via XCM from the system-frontend pallet (i.e., a user on AssetHub who has already had funds withdrawn to fund the tip, per the event doc: "The original sender of the tip (who deposited the funds)"): [1](#0-0) [2](#0-1) 

If the corresponding nonce for `Inbound(nonce)`/`Outbound(nonce)` has already been consumed by the time this call lands on BridgeHub, `InboundQueue::add_tip`/`OutboundQueue::add_tip` return an error, and the amount is moved into `LostTips<T>` instead of being applied to a relayer reward: [3](#0-2) 

The consuming side, `pallet_snowbridge_inbound_queue_v2::AddTip::add_tip`, explicitly gates on `Nonce` already being set for this exact race: [4](#0-3) 

and `process_message` marks the nonce **and consumes any pending tip** atomically as part of ordinary, successful relaying: [5](#0-4) 

The `LostTips` doc comment itself acknowledges the gap: "Capturing the lost tips here supports implementing a recovery method **in the future**" — confirming no such recovery exists yet in this codebase: [6](#0-5) 

No pallet in `bridges/snowbridge/pallets/system-v2` (nor `system-frontend`, `inbound-queue-v2`, `outbound-queue-v2`) exposes a call that reads, refunds, or reassigns `LostTips<T>` — it is write-only from the perspective of on-chain logic.

### Impact Explanation
This meets "Permanent user-fund or bridge-state lock": ordinary users lose real, already-debited value (ether-denominated tip) with zero on-chain path to reclaim it. No malicious peer, relayer, validator, or governance action is required — it is triggered purely by the natural, benign race between a relayer's message-processing transaction and a user's fee-boosting transaction both targeting the same nonce, which is an expected and encouraged usage pattern (users are meant to add tips to speed up relaying of pending messages).

### Likelihood Explanation
High under normal operating conditions: tips are explicitly designed to be added to *pending* (not-yet-relayed) messages to incentivize relayers. Any legitimate user attempting to boost a fee for a message that a relayer submits in the same or an earlier block will trigger this path — no adversarial timing or privileged access is needed, only ordinary transaction-inclusion ordering, which is normal and frequent on a live chain.

### Recommendation
Either (a) add a signed extrinsic allowing the original `sender` account recorded against a `LostTips` entry to withdraw/reclaim their stranded amount, or (b) redesign the flow so the tip amount is only debited from the user after the corresponding nonce is confirmed unconsumed (e.g., an atomic check-and-add across `add_tip` and `process_message`/`process_delivery_receipt`, or a pessimistic reservation the user can unreserve if the tip does not apply).

### Proof of Concept
1. User funds a message tip: system-frontend on AssetHub debits `amount` from the user and dispatches `pallet_bridge_system_v2::add_tip(sender, Inbound(nonce), amount)` over XCM to BridgeHub.
2. In the same or an earlier block, a relayer calls `EthereumInboundQueueV2::submit` for the same `nonce`; `process_message` executes successfully, setting `Nonce::<T>::set(nonce)` and consuming/paying any tip present at that time (none was present yet).
3. The `add_tip` XCM message is then processed: `InboundQueue::add_tip(nonce, amount)` finds `Nonce::<T>::get(nonce) == true` and returns `AddTipError::NonceConsumed`.
4. `pallet_bridge_system_v2::add_tip` catches the error and executes `LostTips::<T>::mutate(&sender, |lost_tip| *lost_tip = lost_tip.saturating_add(amount))`, emitting `TipProcessed { success: false, .. }`.
5. The user's already-debited `amount` now sits permanently in `LostTips::<T>::get(&sender)` with no extrinsic in the repository capable of returning it to `sender`.

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L106-119)
```rust
		/// A tip was processed for an inbound or outbound message, for relayer incentivization.
		/// It could have succeeded or failed (and then added to LostTips).
		TipProcessed {
			/// The original sender of the tip (who deposited the funds).
			sender: AccountIdOf<T>,
			/// The Inbound/Outbound message nonce
			message_id: MessageId,
			/// The tip amount in ether.
			amount: u128,
			/// Whether the tip was added successfully. If the tip was added for a nonce
			/// that was already consumed, the tip will be added to LostTips.
			success: bool,
		},
	}
```

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L136-143)
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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-259)
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
	}
```
