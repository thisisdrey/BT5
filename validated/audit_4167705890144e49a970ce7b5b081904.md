### Title
Snowbridge outbound-queue-v2 `PendingOrders` have no expiry or reap path, permanently locking relayer-reward fees if no `submit_delivery_receipt` is ever submitted - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

### Summary
The Arbitrum report describes a state-machine class of bug: once an entity enters a "waiting" state (presumptive) that depends on an external event (a rival being created) that may never occur, there is no fallback path (e.g., a time-based check) to move the state forward, so the system is stuck forever. The closest verifiable local analog is the `PendingOrder` bookkeeping in `pallet_outbound_queue_v2` (Snowbridge BridgeHub → Ethereum path): once a message is committed and a `PendingOrder` is created with an attached `fee`, the *only* way to resolve/clear that order is `submit_delivery_receipt`/`process_delivery_receipt`, which requires an off-chain relayer to eventually produce a valid Ethereum execution-receipt proof. There is no timeout, expiry, or reap mechanism analogous to the report's recommended "check if it can be confirmed by time" fallback.

### Finding Description
`do_process_message` in [1](#0-0)  creates a `PendingOrder { nonce, fee, block_number }` and inserts it into `PendingOrders<T>` every time an outbound message is accepted. The only code path that ever removes an entry from `PendingOrders` is `process_delivery_receipt`, which is reachable solely through the public extrinsic `submit_delivery_receipt`: [2](#0-1)  and [3](#0-2) .

This mirrors the "presumptive" edge tracker exactly: the pending order is the "presumptive" state, and the trigger to leave that state (`hasRival` in the original report) is here "a relayer eventually submits a valid Ethereum execution receipt for this nonce." If the corresponding Ethereum transaction is never mined, is dropped, reverts for a reason unrelated to nonce validity, or if the message payload commands are structured such that no relayer ever executes them on Ethereum (e.g., the Ethereum-side handler cannot process the command, or the gateway is paused/misconfigured on the Ethereum side), `PendingOrders::<T>::get(nonce)` remains populated indefinitely. There is:
- No `on_idle`/`on_initialize` housekeeping that inspects `PendingOrder.block_number` against a max-age threshold.
- No governance or permissionless "reap stale pending order" extrinsic (unlike, e.g., `pallet_bridge_messages`'s bounded relayer/message confirmation limits, which at least document the expected liveness assumptions).
- No storage bound on `PendingOrders` — it is a plain `StorageMap` with no `MaxEncodedLen`-based cap or stale-entry eviction, so it can grow unbounded across the lifetime of the chain if delivery receipts are ever withheld (whether due to relayer inactivity, Ethereum-side congestion, or a genuinely undeliverable command).

Unlike the message-queue pallet's documented "permanently overweight" scenario (`substrate/frame/message-queue/src/lib.rs:127-138`), which explicitly discusses this failure mode and provides a permissioned but forced fallback (`execute_overweight`), no equivalent mechanism, nor even documentation of the risk, exists for `PendingOrders` in the outbound-queue-v2 pallet.

### Impact Explanation
Every fee amount recorded in a stuck `PendingOrder` is money that is committed to be paid out to a relayer when (and only when) the receipt arrives; because the fee is bound to that specific nonce/order and can only be released via `process_delivery_receipt`, a permanently un-receipted order permanently locks that fee value out of circulation — it can never be reclaimed by the sender nor by any relayer. This matches the "permanent user-fund or bridge-state lock" and "public underpriced work that... stalls bridge processing" impact categories: over time, an accumulation of un-relayed nonces bloats `PendingOrders` storage (state bloat with no bound), while the fees nominally reserved for those messages are permanently unspendable, and no operator-level or protocol-level path exists to clean this up or refund it.

### Likelihood Explanation
This is not attacker-triggered in the sense of needing a malicious relayer/validator — it can occur purely from ordinary real-world conditions (Ethereum-side congestion, an unprofitable relay for small-fee messages, a relayer bug, or a chain reorg on Ethereum invalidating a previously-seen event before finality is proven to the light client). Because relaying is entirely voluntary and economically motivated, any message whose `fee` is too low to be worth relaying will foreseeably never receive a `submit_delivery_receipt`, guaranteeing the "presumptive"-style stall for that nonce's `PendingOrder` indefinitely. This is a systemic gap rather than a rare edge case.

### Recommendation
- Short term: add a time/age-based reap path for `PendingOrders` (e.g., permit anyone to call a `reap_stale_order(nonce)` once `block_number` exceeds some configured age, refunding or burning/redirecting the recorded fee per protocol policy), mirroring the report's short-term recommendation to check whether the pending state "can be confirmed by time."
- Long term: bound `PendingOrders` storage growth and document the expected lifecycle/liveness assumptions for pending orders (as is already done for the message-queue pallet's overweight-message scenario), including what happens to the reserved fee if delivery never occurs.

### Proof of Concept
1. A message is sent through `EthereumBlobExporter`/`snowbridge_pallet_system_v2::Pallet::send`, reaching `do_process_message`, which inserts `PendingOrders::<T>::insert(nonce, order)` with `order.fee = F` [1](#0-0) .
2. No relayer ever calls `submit_delivery_receipt(nonce, ...)` for this nonce (economically unattractive fee, Ethereum congestion, or a stuck/failed transaction on the Ethereum side).
3. `PendingOrders::<T>::get(nonce)` remains `Some(order)` forever; `submit_delivery_receipt`/`process_delivery_receipt` is the only removal path and requires a valid proof of an Ethereum event that will never be produced [4](#0-3) .
4. The reserved fee `F` associated with nonce is permanently neither paid to a relayer nor refunded to the sender, and the entry persists in chain state indefinitely, as there is no expiry/reap call in the pallet (confirmed by inspecting the full pallet call surface: only `submit_delivery_receipt` is exposed) [5](#0-4) .

Note on confidence: I was not able to fully trace, within the available indexed content, exactly where/how the `fee` value inside `Message` is withdrawn from the sender's account before `do_process_message` is invoked (this logic lives in `snowbridge-pallet-system-frontend`/exporter code, which the index only partially surfaced). If fee is *not* pre-charged from the sender but instead drawn from a shared reward pot only at payout time, the "locked user fund" framing narrows to "permanently un-payable relayer obligation and unbounded storage growth" rather than a literal end-user balance lock; the core stuck-state/no-expiry defect and its bridge-processing-stall impact hold regardless. A Devin session with full repository access could verify the exact fee-charging point in `system-frontend`/exporter code to confirm the precise fund-lock semantics.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L293-318)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T>
	where
		<T as frame_system::Config>::AccountId: From<[u8; 32]>,
	{
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
