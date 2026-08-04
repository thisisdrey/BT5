Based on the investigation, I found a concrete structural analog in the Snowbridge outbound-queue-v2 delivery-fee/reward flow.

### Title
Snowbridge outbound-queue-v2 pending delivery fees are permanently locked with no refund path when a message never reaches settlement - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The Snowbridge V2 outbound queue collects an upfront fee for every outbound message and records it in a `PendingOrder`, to be paid out later to whichever relayer submits a valid delivery receipt for that message's nonce. There is no mechanism to refund this fee to the original sender if the message is halted, dropped, or otherwise never executed/receipted on Ethereum — mirroring the reported bug class where fees are committed upfront to a beneficiary and are unrecoverable if the underlying operation is cancelled or never completes.

### Finding Description
When a message is accepted for delivery, `do_process_message` creates and stores a `PendingOrder` keyed by nonce, carrying the fee extracted from the message: [1](#0-0) 

That fee is only ever released via `process_delivery_receipt`, which requires a relayer to submit a verified `DeliveryReceipt` referencing the same nonce; only then is `order.fee` paid out (to the relayer/reward_address) and the `PendingOrders` entry removed: [2](#0-1) 

There is no code path in this pallet that returns `order.fee` to the original message sender if:
- the pallet is halted via `set_operating_mode` (root-only, but the resulting stuck state affects already-accepted messages) — `OperatingMode` is checked elsewhere but has no interaction with already-created `PendingOrders`,
- the message is rejected/reverted on the Ethereum Gateway contract (so no `InboundMessageAccepted`-style delivery event/proof will ever exist for that nonce), or
- no relayer ever bothers to relay the message (economically unprofitable, bridge outage, etc.).

In all these cases the `PendingOrder` for that nonce remains in storage indefinitely: the fee is neither paid to a relayer nor returned to the sender. This is architecturally identical to the reported issue: fees are committed/transferred upfront based on the expectation that a beneficiary will later be paid once an operation completes, but there is no accounting path to reclaim the fee if that completion never happens (the "batch is cancelled" analog here is "the message never gets a delivery receipt").

### Impact Explanation
Under the "Snowbridge delivery flow" and "permanent user-fund or bridge-state lock" categories explicitly named in the impact gate, this results in permanent, unrecoverable loss of the delivery fee for any message that fails to complete end-to-end (bridge halt, relayer inactivity, Ethereum-side revert). Over many messages, this can also cause the `PendingOrders` map to grow unboundedly, since entries are never pruned without a receipt, degrading storage and being effectively "public underpriced work" that stalls bridge processing if honest relayers stop relaying for economically-unfavorable nonces.

### Likelihood Explanation
No malicious actor is required — an unprivileged user's normal message can end up in this state via ordinary conditions (temporary bridge halt via governance, relayer downtime, or the destination Ethereum contract reverting execution for the command). Because the reward is entirely relayer-driven and there is no timeout/expiry/refund extrinsic in this pallet, the condition is reachable purely through the pallet's designed dispatch flow, not through any privileged/attacker action.

### Recommendation
Introduce an expiry mechanism for `PendingOrders`, tracked via the recorded `block_number`, after which the fee can be refunded to the original sender (or claimed back through governance) if no valid delivery receipt has been submitted. This mirrors how `pallet-referenda` handles `refund_submission_deposit`/`refund_decision_deposit` for cancelled/timed-out referenda rather than leaving deposits permanently stuck: [3](#0-2) 

### Proof of Concept
1. A parachain sends a message through `SnowbridgeMessageExporter`/`snowbridge_pallet_system_v2::Pallet::send`, which is validated and delivered into `T::MessageQueue`, ultimately invoking `do_process_message`, which records `PendingOrders::<T>::insert(nonce, order)` with the message's `fee`.
2. Root (or an operational incident) halts the pallet via `set_operating_mode`, or the message is malformed/reverts on the Ethereum Gateway such that no `InboundMessageAccepted`-equivalent receipt event is ever emitted for that nonce.
3. No relayer can ever call `submit_delivery_receipt` successfully for that nonce (verification of an event log that never exists will fail).
4. `PendingOrders::<T>::get(nonce)` remains populated forever; `order.fee` is never paid to any relayer nor returned to the original sender — permanently locked, unlike the fully-refundable model used by `pallet-referenda` and `polkadot/runtime/common/src/auctions` (`cancel_auction`, which unreserves all `ReservedAmounts`) [4](#0-3) .

**Note on verification limits:** I was unable to fully trace, within the available iterations, the exact point in `snowbridge-pallet-system-v2::send` where the message fee is withdrawn from the user's account before `do_process_message` runs — this detail matters for confirming precisely when/how the fee leaves the sender's control. I recommend a Devin session with full repository/tool access to confirm the fee-withdrawal call site and to verify whether any existing timeout/cleanup logic elsewhere (e.g. in `snowbridge-pallet-system-v2` or governance tooling) already mitigates this before treating it as an actionable finding.

### Citations

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

**File:** substrate/frame/referenda/src/lib.rs (L1291-1305)
```rust
	/// Reserve a deposit and return the `Deposit` instance.
	fn take_deposit(
		who: T::AccountId,
		amount: BalanceOf<T, I>,
	) -> Result<Deposit<T::AccountId, BalanceOf<T, I>>, DispatchError> {
		T::Currency::reserve(&who, amount)?;
		Ok(Deposit { who, amount })
	}

	/// Return a deposit, if `Some`.
	fn refund_deposit(deposit: Option<Deposit<T::AccountId, BalanceOf<T, I>>>) {
		if let Some(Deposit { who, amount }) = deposit {
			T::Currency::unreserve(&who, amount);
		}
	}
```

**File:** polkadot/runtime/common/src/auctions/mod.rs (L298-310)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::cancel_auction())]
		pub fn cancel_auction(origin: OriginFor<T>) -> DispatchResult {
			ensure_root(origin)?;
			// Unreserve all bids.
			for ((bidder, _), amount) in ReservedAmounts::<T>::drain() {
				CurrencyOf::<T>::unreserve(&bidder, amount);
			}
			#[allow(deprecated)]
			Winning::<T>::remove_all(None);
			AuctionInfo::<T>::kill();
			Ok(())
		}
```
