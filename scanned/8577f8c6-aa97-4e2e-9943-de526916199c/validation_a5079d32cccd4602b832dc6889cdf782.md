Based on my research, I found a solid local analog to the "ignored external parameter" bug class in Snowbridge's outbound queue v2 delivery receipt handling.

### Title
`process_delivery_receipt` ignores the `success` field of the Ethereum delivery receipt, paying relayer rewards regardless of actual message execution outcome - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
Similar to the Sandclock `receiveFlashLoan` bug — where a fee parameter supplied by an external, trusted-by-protocol caller (`feeAmounts`) is decoded but never validated or incorporated into the settlement logic — the Snowbridge `outbound-queue-v2` pallet's `process_delivery_receipt` function decodes a `DeliveryReceipt` that includes a `success` field, but this field is never checked before releasing the relayer reward from `PendingOrders`.

### Finding Description
The outbound flow stores a `PendingOrder { nonce, fee, block_number }` when a message is queued for Ethereum delivery [1](#0-0) . When a relayer submits a delivery proof, `process_delivery_receipt` is called, decoding a `DeliveryReceipt` (which contains a `success` boolean field per `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs`). The function only validates the gateway address and looks up the `PendingOrder` by nonce, then unconditionally pays the reward if `order.fee > 0`, ignoring `receipt.success` entirely: [2](#0-1) 

The `success` flag — analogous to Balancer's `feeAmounts` — is a value the external system (Ethereum) is expected to communicate back so the receiving contract/pallet can correctly determine settlement, but it's decoded into the struct and then dropped without being used in any conditional path.

### Impact Explanation
If Ethereum-side message execution fails (e.g., `MessageDelivered` but `commands` execution reverted on the Gateway contract), the relayer still collects the full `order.fee` reward as though the message succeeded. This breaks the invariant that "payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" from the required impact list — rewards are settled independent of whether the paid-for work (successful message delivery/execution) actually completed. Over time this incentivizes relayers to submit failed-execution receipts to farm rewards, degrading the intended fee/incentive alignment of the bridge and potentially depleting the rewards pool without corresponding useful bridge work — a form of "public underpriced work that ... stalls bridge processing" once genuine relayers are crowded out by cheap-to-produce failed submissions.

### Likelihood Explanation
This does not require a malicious peer, prover, or governance actor — it only requires an ordinary relayer to submit a `DeliveryReceipt` (with `success: false`) for a nonce that has a real `PendingOrder`. Given `process_delivery_receipt` is a public-facing dispatch path invoked by any relayer with a valid proof, and the check only gates on `T::GatewayAddress::get() == receipt.gateway` and nonce existence, the missing `success` check is directly reachable by an unprivileged actor.

### Recommendation
Add an explicit check on `receipt.success` before crediting `T::RewardPayment::register_reward`, e.g., only pay the reward (or pay a reduced/partial reward) when `receipt.success == true`; otherwise emit a distinct `MessageDeliveryFailed` event and handle the pending order according to protocol semantics (e.g., allow retry, refund, or partial penalty) instead of silently paying full fee regardless of outcome.

### Proof of Concept
1. Queue an outbound message with a non-zero `fee` via `submit`/`Pallet::validate` — a `PendingOrder { nonce, fee, .. }` is inserted.
2. Have Ethereum execution fail (or simply have a relayer construct/prove a receipt where `success: false`, assuming the message-hash verification only binds gateway/nonce/topic, not the execution outcome).
3. Call `process_delivery_receipt(relayer, receipt)` with the crafted receipt.
4. Observe `order.fee > 0` still triggers `T::RewardPayment::register_reward`, and `PendingOrders` is removed — the relayer is rewarded and the order can never be retried/reconciled, despite `receipt.success == false`.

Note: I was unable to fully verify the exact fields of `DeliveryReceipt` and how `success` is used elsewhere (e.g., in emitted events) because the file `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` could not be retrieved in full during this session due to a tool error; the conclusion is based on the `process_delivery_receipt` function body shown above, which does not reference `receipt.success` in any branch of the reward-payment logic. Starting a Devin session against the full repository would allow confirming the complete `DeliveryReceipt` struct and any downstream usage of `success` outside this function.

### Citations

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
