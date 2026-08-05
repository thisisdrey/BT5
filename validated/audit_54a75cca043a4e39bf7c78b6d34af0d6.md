Audit Report

## Title
Snowbridge `pallet_outbound_queue_v2` `PendingOrders` entries have no expiry or reap path, permanently locking relayer-reward fees if `submit_delivery_receipt` is never submitted - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`do_process_message` inserts a `PendingOrder { nonce, fee, block_number }` into the unbounded `PendingOrders<T>` `StorageMap` for every outbound message accepted, and the fee (WETH bridged from the sender via AssetHub) is committed to reward whichever relayer eventually proves delivery. The only removal path for a `PendingOrders` entry is `process_delivery_receipt`, reachable exclusively via the public `submit_delivery_receipt` extrinsic, which requires a valid Ethereum execution-receipt proof; there is no time-based expiry, reap extrinsic, or `on_idle`/`on_initialize` housekeeping, so any message that is never relayed (economically unattractive fee, Ethereum-side congestion, reverted/dropped Ethereum transaction, etc.) leaves its `PendingOrder` — and the fee committed to it — permanently stuck, while the map grows without bound.

## Finding Description
In `do_process_message` [1](#0-0) , every accepted outbound message causes a `PendingOrder` to be inserted keyed by `nonce` into `PendingOrders<T>`, which is declared as a plain, unbounded `StorageMap` [2](#0-1) . The pallet's only call surface for clearing this map is `submit_delivery_receipt`, which verifies an Ethereum event-log proof and then calls `process_delivery_receipt` [3](#0-2) . `process_delivery_receipt` fetches the order by nonce, pays the relayer reward via `T::RewardPayment::register_reward`, and only then removes the entry with `<PendingOrders<T>>::remove(nonce)` [4](#0-3) . The pallet's `Hooks` implementation only clears the per-block `Messages`/`MessageLeaves` storage and performs no age-based cleanup of `PendingOrders` [5](#0-4) . Per the pallet's own design documentation, the fee is bridged in as WETH and is intended to be claimed only once a relayer supplies a valid delivery proof [6](#0-5) ; there is no code path to refund the sender or otherwise release the fee if that proof never arrives.

## Impact Explanation
Because the only state transition out of "pending" requires an external, voluntary relayer action gated on Ethereum-side execution succeeding and being provable, any message whose associated fee is not economically attractive to relay (or whose Ethereum-side transaction is dropped/reverted/never mined) leaves its `PendingOrder` — and the WETH fee value bound to it — permanently unresolved: not paid to any relayer and not returned to the sender. This matches the "permanent user-fund or bridge-state lock" impact category, and additionally causes indefinite, unbounded growth of `PendingOrders` state with no bound or reap mechanism analogous to the message-queue pallet's `execute_overweight` fallback for permanently-overweight messages.

## Likelihood Explanation
This does not require a malicious actor: any ordinary user submitting a bridge message with a low or borderline relayer fee, combined with normal, foreseeable conditions (Ethereum congestion, an unprofitable relay, a dropped/reverted transaction, or a relayer simply never appearing for that nonce) will trigger this state. The condition is systemic rather than a rare edge case, and it is fully reachable through the pallet's normal, public message-submission and `submit_delivery_receipt` flow with no privileged actions required.

## Recommendation
Add a permissionless, time/age-gated reap mechanism (e.g., a `reap_stale_order(nonce)` call, or `on_idle` housekeeping) that inspects `PendingOrder.block_number` against a configurable maximum age and either refunds the fee to the original sender or defines and documents an explicit disposition policy for the fee, mirroring the "check if it can be confirmed by time" fallback pattern. Additionally, bound `PendingOrders` growth or document its expected liveness assumptions similarly to the message-queue pallet's overweight-message handling.

## Proof of Concept
1. Submit a bridge message from AssetHub to Ethereum via the standard XCM flow, reaching `do_process_message`, which inserts `PendingOrders::<T>::insert(nonce, order)` with `order.fee = F`.
2. Do not call `submit_delivery_receipt(nonce, ...)` for this nonce (e.g., because no relayer finds it profitable, or the Ethereum-side transaction never confirms).
3. Observe that `PendingOrders::<T>::get(nonce)` remains `Some(order)` indefinitely; the only call that could clear it, `submit_delivery_receipt`, requires a valid Ethereum receipt proof that is never produced.
4. Confirm via `Call` inspection that `submit_delivery_receipt` is the pallet's only extrinsic and no expiry/reap logic exists in `Hooks::on_initialize`/`on_finalize`.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L268-271)
```rust
	/// Pending orders to relay
	#[pallet::storage]
	pub type PendingOrders<T: Config> =
		StorageMap<_, Twox64Concat, u64, PendingOrder<BlockNumberFor<T>>, OptionQuery>;
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L273-286)
```rust
	#[pallet::hooks]
	impl<T: Config> Hooks<BlockNumberFor<T>> for Pallet<T> {
		fn on_initialize(_: BlockNumberFor<T>) -> Weight {
			// Remove storage from previous block
			Messages::<T>::kill();
			MessageLeaves::<T>::kill();
			// Reserve some weight for the `on_finalize` handler
			T::WeightInfo::on_initialize() + T::WeightInfo::commit()
		}

		fn on_finalize(_: BlockNumberFor<T>) {
			Self::commit();
		}
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L298-317)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::submit_delivery_receipt())]
		pub fn submit_delivery_receipt(
			origin: OriginFor<T>,
			event: Box<EventProof>,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			let relayer = ensure_signed(origin)?;

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			let receipt = DeliveryReceipt::try_from(&event.event_log)
				.map_err(|_| Error::<T>::InvalidEnvelope)?;

			Self::process_delivery_receipt(relayer, receipt)
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-480)
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
```

**File:** bridges/snowbridge/docs/v2.md (L152-169)
```markdown
### Step 6: Relayer relays message to Gateway

1. A relayer _Charlie_ inspects storage $P$ to look for new messages to relay. Suppose it finds $\mathrm{hash}(m)$
   giving reward $r$.
2. The relayer queries $m$ from $M$ and constructs the necessary proofs.
3. The relayer dry-runs m on Ethereum to decide whether the message is profitable to deliver.
4. The relayer finally delivers the message together with a relayer-controlled address $u$ on AH where the relayer can
   claim their reward after proof of delivery.

### Step 7: Relayer delivers proof of delivery to BH

The proof of delivery is essentially a merkle proof for the `InboundMessageAccepted` event log.

When BH processes the proof of delivery:

1. The command $m$ is removed from storage items $M$ and $P$.
2. The relayer reward is tracked in storage $R$, where $R(u)$ is the accumulated rewards that can be claimed by account
   $u$.
```
