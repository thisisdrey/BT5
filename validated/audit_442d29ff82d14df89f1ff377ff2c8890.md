Based on my investigation, I found a concrete local analog. The external report's core broken invariant is: **a result/output value that should gate downstream fund movement is silently ignored, so accounting proceeds as if the value were always in the "safe" state, and value moves incorrectly as a result.**

The matching pattern in this repository is in Snowbridge's outbound queue v2 reward payout path.

### Title
Snowbridge `process_delivery_receipt` pays relayer reward without validating `DeliveryReceipt::success` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` in the Snowbridge outbound-queue-v2 pallet is responsible for paying a relayer reward once a message has supposedly been delivered to Ethereum. The `DeliveryReceipt` struct carries a `success` field, but this field is never consulted before the reward is registered and the pending order is removed.

### Finding Description
The function reads the pending order for the given nonce, and if `order.fee > 0`, unconditionally calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)`, then removes the order from `PendingOrders`: [1](#0-0) 

The `receipt.success` boolean (present in the `DeliveryReceipt` type used as the function's own parameter) is never checked anywhere in this code path — only `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce` are read. This mirrors the ERC721Pool bug class exactly: a struct field that is meant to gate/inform a downstream state transition (`result.remainingCollateral` in the Solidity report, `receipt.success` here) is populated by the proof-verification/decoding step but never read by the code that performs the payout/settlement, so the settlement logic behaves as though the field were always in its default/success state.

Because `PendingOrders` is removed unconditionally in the same call, this is a single, one-shot, irreversible action per nonce: as soon as a syntactically valid, proof-verified receipt for a nonce is submitted — even one whose `success` flag indicates the message execution on Ethereum actually failed — the reward is paid out and the order entry (the only on-chain record that a reward is still owed/pending) is deleted.

### Impact Explanation
This breaks the "duplicate settlement / payout" and "public underpriced work" invariant classes called out in the task's pivots: message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically. Here, settlement (reward registration) is decoupled from the receipt's own success indicator. A failed delivery still results in the relayer being paid the full `order.fee`, and the bridge loses the ability to ever re-attempt or re-account for that nonce since `PendingOrders` is cleared regardless of outcome. This is a runtime bug that compromises intended bridge reward/settlement behavior without needing a malicious relayer, validator, or governance actor — the extrinsic-calling relayer only needs a real (Ethereum-side) receipt log with `success: false` and a valid Merkle/receipt proof, which is achievable by anyone whose message execution on Ethereum genuinely reverted or failed.

### Likelihood Explanation
The likelihood is high for any relayer whose submitted message fails execution on the Ethereum side (e.g., due to gas issues, revert in the destination contract call, or `Transact`/agent execution failure) — a normal, permissionless, non-adversarial occurrence in bridge operation, not requiring any privileged access. The proof verification path (`submit_delivery_receipt` extrinsic, not shown here) only authenticates that the receipt log exists on Ethereum and decodes it; it does not add extra logic to gate on `success`. Since `process_delivery_receipt` is the sole consumer of the decoded receipt for payout purposes, the missing check directly and reliably reaches the reward-payment code path.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before calling `T::RewardPayment::register_reward`. On `success == false`, either: (a) do not pay the reward and instead emit a `MessageDeliveryFailed`-style event, retaining or explicitly clearing `PendingOrders` per the intended failure-handling policy; or (b) route to a distinct failure-accounting path so that reward funds are conserved and settlement state accurately reflects execution outcome. This mirrors the ERC721Pool fix of explicitly propagating the omitted field (`result.remainingCollateral = borrower.collateral`) into the code path that consumes it.

### Proof of Concept
1. A message is queued via the normal outbound flow, generating a `PendingOrders` entry with a non-zero `fee` for `nonce = N`.
2. On the Ethereum side, the message execution reverts/fails (e.g., the target contract call panics), so the actual delivery receipt log has `success = false`.
3. A relayer constructs a valid Merkle/receipt proof for this failed-execution log and calls the pallet's `submit_delivery_receipt` extrinsic, which internally invokes `process_delivery_receipt(relayer, receipt)` with `receipt.success == false`.
4. Because `process_delivery_receipt` only checks `order.fee > 0` (see [2](#0-1) ) and never inspects `receipt.success`, `T::RewardPayment::register_reward` is called and the reward is registered to `reward_account`.
5. `PendingOrders::<T>::remove(nonce)` deletes the order, and a `MessageDelivered` event is emitted — even though the message delivery genuinely failed — permanently paying out funds for a failed/underpriced/incomplete unit of relayer work. [1](#0-0)

### Citations

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
