## Analysis

The bug-class from the report is: **an append-only, unbounded on-chain collection that is populated by ordinary (unprivileged) usage, but has no removal/expiry path except one gated by an external, hard-to-guarantee action** — leading to unbounded storage growth and eventual operational failure (DoS) that nobody can economically or mechanically reverse.

The closest verifiable analog in this repository is the `PendingOrders` map in the Snowbridge V2 outbound queue pallet.

### Title
Unbounded, un-prunable growth of `PendingOrders` in Snowbridge outbound queue v2 - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
Every message the outbound queue processes (from any sibling parachain via XCM, or from `snowbridge-pallet-system-v2`) inserts a permanent entry into the `PendingOrders` storage map, keyed by `nonce`. The only way an entry is ever removed is if a relayer later submits a cryptographically-verified `submit_delivery_receipt` for that exact nonce. There is no timeout, no cap on total entries, and no alternative pruning mechanism, mirroring exactly the `vestingRecipients` pattern in the seed report: an ever-growing collection with disposal gated behind a condition (claim/withdraw or, here, delivery-receipt) that is not guaranteed to occur for every entry.

### Finding Description
`do_process_message` unconditionally inserts a new `PendingOrder` for every processed message: [1](#0-0) 

Removal only happens inside `process_delivery_receipt`, which requires a valid Ethereum-side proof (`T::Verifier::verify`) and a matching nonce that must already exist in `PendingOrders`: [2](#0-1) 

There is no code path anywhere in the pallet (hooks, other extrinsics, or migrations) that expires, prunes, or garbage-collects stale `PendingOrders` entries — confirmed by the absence of any stale/expire/prune logic in the Snowbridge tree. Compare this to the analogous, already-bounded pattern used in `bridges/modules/messages`, where unrewarded relayer entries are explicitly capped by `MAX_UNREWARDED_RELAYERS_IN_CONFIRMATION_TX` precisely to prevent unbounded growth: [3](#0-2) 

No equivalent bound exists for `PendingOrders`. Any message whose corresponding Ethereum-side event is never observed/relayed (execution reverts on Ethereum, the relayer never submits, the nonce is unprofitable to claim because `fee == 0`, or the gateway/verifier is temporarily halted per `pr_11856.prdoc`) leaves a permanent, un-removable entry. Since outbound messages can originate from any sibling parachain's XCM traffic (a public, non-privileged trigger), an attacker (or simply organic usage over time) can grow `PendingOrders` without bound, and crucially, without paying any storage deposit proportional to this permanent state — the entry is "free" beyond the ordinary message-processing fee.

### Impact Explanation
This directly matches the accepted impact category "public underpriced work that degrades block production or stalls bridge processing": the sender of an outbound message pays only for message processing, not for the permanent state growth of `PendingOrders`. Because BridgeHub is a system parachain with PoV/state-size constraints, continuously growing unbounded state increases storage costs and long-term chain state size with no offsetting mechanism, degrading over time exactly like the seed report's ever-growing `vestingRecipients` array threatening `allVestingRecipients()`.

### Likelihood Explanation
High. No adversarial relayer, prover, or governance action is required — a single unprofitable/unrelayed message (fee = 0, dropped Ethereum execution, or a period where the verifier is halted per the referenced prdoc) is enough to create a permanently orphaned entry. Over the lifetime of the bridge, this is a near-certainty for at least some fraction of messages, since delivery-receipt submission is entirely dependent on independent, unincentivized off-chain relayer behavior.

### Recommendation
Introduce a bounded/expiring mechanism for `PendingOrders`, e.g.:
- Add a `block_number`-based expiry check (the field already exists on `PendingOrder`) and reap orders older than a configurable threshold via `on_initialize`/`on_idle`, bounded per block.
- Alternatively, charge a per-entry storage deposit at insertion time, refunded upon `submit_delivery_receipt`, so abandoned entries at least don't represent an unpriced negative externality.
- Cap the maximum number of outstanding `PendingOrders` and reject/backpressure new message processing once the cap is reached, analogous to `MAX_UNREWARDED_RELAYERS_IN_CONFIRMATION_TX` in `pallet-bridge-messages`.

### Proof of Concept
1. Have any sibling parachain (or the bridge system pallet) send an outbound message with `fee = 0` through the normal XCM export path.
2. `do_process_message` executes and unconditionally calls `<PendingOrders<T>>::insert(nonce, order)`. [1](#0-0) 
3. Because `fee == 0`, no relayer has an economic incentive to call `submit_delivery_receipt` for this nonce; the entry is therefore never removed by `process_delivery_receipt`. [4](#0-3) 
4. Repeat step 1 across blocks — `PendingOrders` accumulates permanently with no bound and no pruning code path anywhere in the pallet.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-438)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-476)
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

```

**File:** bridges/chains/chain-cumulus/src/lib.rs (L129-144)
```rust
// According to (preliminary) weights of messages pallet, cost of additional message is zero and the
// cost of additional relayer is `8_000_000 + db read + db write`. Let's say we want no more than
// 4096 unconfirmed messages (no any scientific justification for that - it just looks large
// enough). And then we can't have more than 4096 relayers. E.g. for 1024 relayers is (using
// `RocksDbWeight`):
//
// `1024 * (8_000_000 + db read + db write) = 1024 * (8_000_000 + 25_000_000 + 100_000_000) =
// 136_192_000_000`
//
// So 1024 looks like good approximation for the number of relayers. If something is wrong in those
// assumptions, or something will change, it shall be caught by the
// `ensure_able_to_receive_confirmation` test.

/// Maximal number of unrewarded relayer entries at inbound lane for Cumulus-based parachains.
/// Note: this value is security-relevant, decreasing it should not be done without careful
/// analysis (like the one above).
```
