Based on my research, I found a solid structural analog to the "no incentive to liquidate small positions" bug class in Snowbridge's outbound message delivery/reward pipeline.

### Title
Zero/low-fee outbound messages can be permanently stranded because relayers have no incentive to submit delivery receipts - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

### Summary
`OutboundQueue::do_process_message` accepts and commits a message to the Ethereum-bound merkle root using whatever `fee` value is embedded in the decoded `Message`, without enforcing any protocol-level minimum reward, and `process_delivery_receipt` only pays the relayer `if order.fee > 0`. [1](#0-0) [2](#0-1) 

### Finding Description
Every accepted outbound message is unconditionally committed into `Messages`/`MessageLeaves` and thus into the Merkle root delivered to Ethereum, and a `PendingOrder{nonce, fee, block_number}` is created regardless of how small `fee` is. [3](#0-2) 
Relayers are only economically motivated to call `submit_delivery_receipt` because they are paid `order.fee` via `T::RewardPayment::register_reward` once they prove delivery on Ethereum. If a message carries a fee of zero (or a value smaller than the relayer's gas cost to submit the finality/receipt proof on Ethereum and the delivery-receipt extrinsic on BridgeHub), no rational relayer will ever deliver it — this is functionally identical to the reported DSCEngine issue where liquidators won't act on small, unprofitable positions.

The maintainers themselves acknowledged this exact bug class: PR #8271 explicitly states "the relayer reward is too low and not profitable to process" and introduces an `add_tip`/`Tips` top-up mechanism as a mitigation. [4](#0-3) 
That mitigation is opt-in and reactive (someone else must notice the stuck message and manually call `add_tip` via `pallet-system-v2::add_tip`), not a structural fix — messages with `fee == 0` are still accepted and committed at enqueue time with no minimum-fee floor check inside `do_process_message`. [5](#0-4) 
This exact area of the codebase has also already had a related correctness regression: PR #9746 documents that relayer tips could be "lost since it had already been burnt," confirming the reward/incentive plumbing here is fragile. [6](#0-5) 

Because the underlying asset transfer (e.g. reserve/burn on the Polkadot side via the XCM that produced the outbound message) has already executed by the time the message is enqueued, an underpriced message leaves the corresponding value in a "sent but undeliverable" limbo state on BridgeHub — the `PendingOrder` sits in storage indefinitely with no relayer willing to claim it, and the user's transferred assets are never released/minted on Ethereum.

### Impact Explanation
This matches the "permanent user-fund or bridge-state lock" and "public underpriced work that degrades ... stalls bridge processing" categories in the impact gate: an unprivileged user (or a misconfigured/underfunded fee calculation path) can cause funds to be irrecoverably stuck, and accumulating unpaid `PendingOrders` bloats bridge state that nothing will ever clean up (since removal only happens in `process_delivery_receipt`, which requires an incentivized relayer to act).

### Likelihood Explanation
Likelihood is elevated because: (1) the maintainers' own PR description confirms this is a known, real, previously-unmitigated economic condition; (2) the mitigation (`add_tip`) is optional and requires a third party to notice and act, meaning the default/no-attention path leaves the message permanently unrelayed; (3) it requires no privileged actor, malicious relayer, or governance action — any ordinary user whose XCM fee computation underestimates the remote gas/reward component (or who interacts with a path that sets `fee: 0`, as seen in the outbound-queue-v2 benchmarking mock) can trigger it. [7](#0-6) 

### Recommendation
Enforce a protocol-defined minimum `fee`/reward at the point messages are accepted in `do_process_message` (mirroring the `calculate_fee`/`Params.Reward` floor already documented for the v1 outbound queue), rejecting or requiring top-up before commitment rather than after the message is already irreversibly included in the Merkle root. Alternatively, require the `Tips`/`add_tip` top-up to occur atomically as a precondition for enqueueing rather than as an after-the-fact rescue mechanism, and add a bounded-time recovery/refund path for `PendingOrders` that remain unclaimed past a timeout so locked value is not permanent. [8](#0-7) 

### Proof of Concept
1. A user (or a mis-priced XCM `InitiateAssetsTransfer`) causes an outbound v2 message to be enqueued with `Message.fee = 0` (or a negligible non-zero value).
2. `do_process_message` accepts it unconditionally, commits it to `MessageLeaves`, and stores `PendingOrder { nonce, fee: 0, .. }` in `PendingOrders`. [9](#0-8) 
3. Because `fee == 0`, `process_delivery_receipt` would pay no reward even if a relayer submitted a receipt (`if order.fee > 0`), so no rational relayer submits the Ethereum-side transaction to fulfill the message and no relayer submits `submit_delivery_receipt`. [10](#0-9) 
4. The `PendingOrder` remains in storage forever (nothing times it out), and the value already moved/locked on the source chain for that transfer is never realized on Ethereum, unless an uninvolved third party later notices and manually calls `add_tip` to subsidize it.

Note: I was unable to fully inspect `send_message_impl.rs` (the code path that computes/validates `Message.fee` before enqueueing) due to a tool failure in the final research iteration, so I cannot confirm with certainty whether there is *any* existing minimum-fee check upstream of `do_process_message`. If such a check exists and enforces a realistic floor, this reduces to the "reactive top-up only" issue described in PR #8271/#9746 rather than a fully open zero-fee path; this should be verified directly in that file before treating the finding as fully proven.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L403-443)
```rust
			Messages::<T>::append(outbound_message);

			// Convert it to an OutboundMessageWrapper (in ABI format), hash it using Keccak256 to
			// generate a committed hash, and store it in MessageLeaves storage which can be
			// verified on Ethereum later.
			let abi_commands: Vec<CommandWrapper> = commands
				.into_iter()
				.map(|command| CommandWrapper {
					kind: command.kind,
					gas: command.gas,
					payload: Bytes::from(command.payload),
				})
				.collect();
			let committed_message = OutboundMessageWrapper {
				origin: FixedBytes::from(origin.as_fixed_bytes()),
				nonce,
				topic: FixedBytes::from(id.as_fixed_bytes()),
				commands: abi_commands,
			};
			let message_abi_encoded_hash =
				<T as Config>::Hashing::hash(&committed_message.abi_encode());
			MessageLeaves::<T>::append(message_abi_encoded_hash);

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

			Ok(true)
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L462-473)
```rust
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
```

**File:** prdoc/stable2506/pr_8271.prdoc (L1-7)
```text
title: Snowbridge - Message reward topups
doc:
- audience: Runtime Dev
  description: |-
     This PR enables the ability to add a tip to an Inbound or Outbound message, in case the relayer reward is too low
     and not profitable to process. The tip is added to the relayer reward when processing a message.

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

**File:** prdoc/stable2509/pr_9746.prdoc (L1-7)
```text
title: Snowbridge Inbound Queue V2 relayer tip payout fix

doc:
- audience: Runtime Dev
  description: |
    Fixes a bug where relayer tips were not properly paid out, causing the tips to be lost since it had already been
    burnt.
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/benchmarking.rs (L37-42)
```rust
		let message = Message {
			origin: Default::default(),
			id: H256::default(),
			fee: 0,
			commands: BoundedVec::try_from(commands.clone()).unwrap(),
		};
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L60-67)
```rust
//! ## Fee Computation Function
//!
//! ```text
//! LocalFee(Message) = WeightToFee(ProcessMessageWeight(Message))
//! RemoteFee(Message) = MaxGasRequired(Message) * Params.MaxFeePerGas + Params.Reward
//! RemoteFeeAdjusted(Message) = Params.Multiplier * (RemoteFee(Message) / Params.Ratio("ETH/DOT"))
//! Fee(Message) = LocalFee(Message) + RemoteFeeAdjusted(Message)
//! ```
```
