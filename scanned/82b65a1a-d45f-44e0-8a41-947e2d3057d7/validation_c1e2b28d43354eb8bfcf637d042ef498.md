## Finding: Reward payout state advances unconditionally, ignoring the `success` field of the Ethereum delivery receipt

### Title
Unconditional relayer reward payout regardless of `DeliveryReceipt.success` in `EthereumOutboundQueueV2::process_delivery_receipt` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The Tapioca report's core broken invariant is: a field/flag that must be consulted to determine how a downstream operation should be applied (`unwrap`) was hard-coded to a value that ignores the actual state, so the receiving logic executed incorrectly. The local analog is in Snowbridge's V2 outbound queue: `DeliveryReceipt` carries a `success` field describing whether the message was actually executed successfully on Ethereum, but `Pallet::process_delivery_receipt` never reads or branches on it before paying the relayer reward and clearing the pending order.

### Finding Description
`submit_delivery_receipt` verifies the event log/proof via `T::Verifier::verify` and decodes it into a `DeliveryReceipt` (which includes a `success: bool` field, confirmed by its usage in integration tests, e.g. `DeliveryReceipt { gateway, nonce, reward_address, topic, success: true }`). [1](#0-0) 

That decoded receipt is passed straight into `process_delivery_receipt`, which:
1. Checks the gateway address matches.
2. Resolves the reward account.
3. Looks up the `PendingOrder` by nonce.
4. Unconditionally calls `T::RewardPayment::register_reward(...)` for `order.fee` if `order.fee > 0`.
5. Unconditionally removes the `PendingOrders` entry, permanently finalizing the settlement for that nonce. [2](#0-1) 

At no point is `receipt.success` read. The field exists in the wire format and is populated from the on-chain Ethereum event, but the pallet's settlement logic treats every verified receipt as an equally valid "delivered" event, regardless of whether the actual message execution reverted on the Ethereum Gateway contract.

### Impact Explanation
This directly violates the pivot: "Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Here, decode and proof-verification succeed, but *execution* success (the actual semantic meaning of `success`) is not gated before advancing the payout state (`register_reward`) and permanently removing the `PendingOrder` (`PendingOrders::remove(nonce)`). Once removed, there is no other path to re-evaluate or contest that nonce — the settlement is final and irreversible, and reward payment is decoupled from whether the bridged operation for which the fee was collected ever actually completed on the destination chain. This can misallocate protocol-controlled reward funds to relayers for messages whose execution failed, breaking the value-conservation intent of the reward pool tied to real successful delivery/execution.

### Likelihood Explanation
Any relayer can trigger this path by observing a genuine (but reverted) execution on Ethereum, submitting a legitimately provable event log/proof (the `Verifier::verify` step only checks authenticity of the log, not the semantic outcome), and calling the public, unprivileged `submit_delivery_receipt` extrinsic. No malicious relayer/validator collusion, governance action, or leaked key is required — an honest relayer submitting a proof for a reverted execution is sufficient to trigger unconditional reward payout, since the code path contains no branch on `success`.

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only call `T::RewardPayment::register_reward` when `receipt.success` is `true`. For the `false` case, define explicit failure-handling semantics (e.g., different/reduced reward, retry logic, or a distinct rejected event) before removing the `PendingOrder`, so payout state advancement is contingent on confirmed successful execution, not merely on the receipt being decodable and its proof being valid.

### Proof of Concept
1. A message is enqueued via `do_process_message`, creating `PendingOrders[nonce] = PendingOrder { fee, ... }`. [3](#0-2) 
2. The message is delivered to the Ethereum Gateway, but its embedded command execution reverts on-chain (still emits a delivery event log, with `success = false`).
3. A relayer obtains the transaction receipt/event log and a valid Merkle/verifier proof for that log (this is achievable for any real, mined transaction regardless of its execution outcome) and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the log is genuine), `DeliveryReceipt::try_from` decodes `success: false`, but `process_delivery_receipt` never inspects it: [4](#0-3) 
5. `order.fee` is paid to `reward_account` via `register_reward`, and `PendingOrders::remove(nonce)` finalizes the settlement — identical to the success case — even though the bridged operation never completed on Ethereum.

Note: I was unable to fully inspect `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` (the exact `DeliveryReceipt` struct definition and how `success` is derived from the log) before running out of tool calls; its existence and use as `success: true`/`success: false` is confirmed only via test call sites and grep matches, not the full struct/decoding logic. A Devin session with full repo access could confirm the exact derivation of `success` from the Ethereum event log and validate this proof of concept end-to-end.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-440)
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

			<Nonce<T>>::set(nonce);

			Self::deposit_event(Event::MessageAccepted { id, nonce });
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
