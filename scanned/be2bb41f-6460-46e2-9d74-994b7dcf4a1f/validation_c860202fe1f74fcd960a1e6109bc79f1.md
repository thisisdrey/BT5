## Local Analog Found

Based on my investigation, the strongest local analog to the reported "unchecked transfer result" bug class is in the **Snowbridge outbound queue v2** delivery-receipt handling, where a relayer reward is paid and a pending order is finalized purely from the presence of a verified event log — without checking whether the underlying dispatch/command execution on Ethereum actually succeeded.

### Title
Outbound queue v2 pays relayer reward and settles pending orders without checking the delivery receipt's execution-success outcome - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::submit_delivery_receipt` verifies only that the event log/proof pair is cryptographically valid and matches the expected Gateway address, then unconditionally pays the relayer reward and clears the `PendingOrders` entry for the nonce. It never inspects whether the command(s) referenced by that nonce actually executed successfully on the Ethereum side — mirroring the audited bug where an ERC20 `transfer`'s boolean return value is ignored, letting the bridge "settle" (emit `Withdrawal`) despite failure.

### Finding Description
The extrinsic flow is: [1](#0-0) 

`Self::process_delivery_receipt` then does: [2](#0-1) 

The function only validates `receipt.gateway == T::GatewayAddress::get()`, looks up `PendingOrders` by `receipt.nonce`, and — if `order.fee > 0` — calls `T::RewardPayment::register_reward(...)` and removes the order. Nowhere in this path is any execution-success/outcome field of the decoded `DeliveryReceipt` consulted before paying the reward and permanently deleting the pending order. This is structurally identical to the reported pattern: a state-changing "settlement" step (`used[txHash] = true` / `emit Withdrawal` in the report vs. `register_reward` + `PendingOrders::remove` here) is performed based on the mere fact that an operation was *attempted and logged*, not on whether the payload it represents actually succeeded.

### Impact Explanation
If the on-chain `DeliveryReceipt` type does not encode (or this handler does not consult) a genuine success/failure indicator, a relayer can obtain proof of *any* Gateway-emitted event for a given nonce — including one corresponding to a reverted/failed command dispatch on Ethereum — and still collect the relayer reward while the `PendingOrder` is deleted forever. Because `PendingOrders` is the only bookkeeping used to reconcile outbound message delivery, once it is removed there is no other on-chain record to detect or retry the failure; this can result in permanent loss of the reward funds pool value to relayers for undelivered work, and it removes the chain's own record of an outstanding cross-chain obligation, mirroring the "funds locked / incorrect Withdrawal emission" outcome described in the report.

### Likelihood Explanation
Any account holding the reward can call `submit_delivery_receipt` — it requires only `ensure_signed`, not a privileged or trusted relayer role, and a normal SNOWBRIDGE relayer already needs to construct exactly this kind of event-log + proof pair for legitimate purposes, so the same tooling can be pointed at whatever log the Gateway happens to emit for a failed dispatch.

### Recommendation
Confirm that the Ethereum Gateway contract's dispatch-completion event includes an explicit success/failure flag, ensure `DeliveryReceipt::try_from` decodes it, and require `process_delivery_receipt` to branch on that flag: only pay the reward and clear `PendingOrders` on a genuine success outcome; on failure, either retain the order for retry/refund handling or route to an explicit failure-settlement path that does not reward the relayer.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce]` with a non-zero `fee`.
2. The corresponding command fails/reverts during execution on the Ethereum Gateway (analogous to an ERC20 `transfer` returning `false` in the audited report), but the Gateway still emits a log for the nonce (e.g., a generic "dispatched" event).
3. A relayer obtains the standard proof for this log and submits `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (the log/proof pair is valid), `DeliveryReceipt::try_from` decodes it, and `process_delivery_receipt` proceeds to call `register_reward` and `PendingOrders::remove(nonce)` without any success check — settling and rewarding a failed delivery exactly as in the original report's exploit scenario.

**Note:** I was not able to inspect the full `DeliveryReceipt` struct definition (`bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs`) or the Gateway contract's Solidity event definition within this session to confirm definitively whether a success field exists and is silently dropped, versus the event only being emitted on success by contract design. This should be verified directly against `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` and the corresponding Solidity Gateway contract before treating this as fully confirmed; if the Gateway only emits this specific event on successful command execution, the analog does not hold and the finding should be discarded.

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
