Based on my research, the key finding is in `process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` (lines 445-480).

### Title
`process_delivery_receipt` settles reward by nonce alone without binding to the committed message hash - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The external report's core broken invariant is: two code paths that are supposed to verify/consume the *same logical committed data* (the same payload) bind their hash/commitment differently, so one path accepts data that the other would reject as mismatched. In the Snowbridge outbound-queue-v2 flow I found an analogous binding gap: at commit time the pallet computes and stores a Keccak256 leaf hash over the full ABI-encoded `OutboundMessage` (`do_process_message`, `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:355-359`, referenced also for the non-v2 pallet), but at settlement time (`process_delivery_receipt`, lines 445-480) the relayer-controlled `DeliveryReceipt` is accepted and a reward is paid out purely by looking up `PendingOrders` **by `nonce`**, checking only `receipt.gateway == GatewayAddress`. There is no re-derivation/comparison of the receipt's `topic` (which is meant to identify the delivered message) against the actual committed message hash/leaf for that nonce.

### Finding Description
`DeliveryReceipt` (`bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs:14-27`) is decoded from an `InboundMessageDispatched` Ethereum event log and carries `gateway`, `nonce`, `topic`, `success`, and `reward_address`. `submit_delivery_receipt` (`lib.rs:298-317`) verifies only that the *event log* itself is included in a valid Ethereum receipt (via `T::Verifier::verify`), then calls `process_delivery_receipt`.

`process_delivery_receipt` (`lib.rs:445-480`) does:
```rust
ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
...
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
```
`receipt.topic` — the value that, on the outbound side, is supposed to identify *which* committed message was actually dispatched on Ethereum — is decoded but never checked against anything stored on-chain for that `nonce` (no comparison to a stored message id/hash in `PendingOrder` or `Messages`). The only fields that gate the payout are `gateway` and `nonce`. Since the Ethereum Gateway contract is the entity that emits `InboundMessageDispatched(nonce, topic, success, reward_address)`, the pallet trusts the *event's nonce* field as the sole correlation key to the previously-committed `PendingOrder`, without cross-checking that the emitted `topic` matches the message id/hash that was actually committed for that nonce on BridgeHub. This is the same class of bug as the report: one place (commit time) computes a strong per-message hash/id, and the other place (settlement time) is willing to accept/settle using a weaker binding (nonce only, ignoring the topic/hash field that should be checked for correlation).

### Impact Explanation
If the Gateway/relay-side event's `topic` is not required to match the BridgeHub-side committed message id, a mismatch between the two "hash" surfaces (commit-time strong id vs settlement-time weak nonce-only key) means the settlement logic cannot detect topic/message substitution. In the worst case this allows a relayer-submitted receipt for nonce `N` to be honored for reward purposes even if the actual dispatched Ethereum event's `topic` does not correspond to the message BridgeHub actually committed under `PendingOrders[N]` — a duplicate-settlement/wrong-correlation risk consistent with the "Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot.

### Likelihood Explanation
This requires that `nonce` values are the only correlation key and that no additional runtime check (elsewhere, e.g. in the `Verifier` or Gateway contract semantics not visible in this Rust codebase) enforces topic-nonce binding. I could not fully confirm from the Solidity/Gateway side (out of scope of this repo) whether `nonce` uniqueness there structurally prevents any exploitable mismatch, so likelihood is **uncertain** — this is a plausible but not fully proven analog given the available code.

### Recommendation
In `process_delivery_receipt`, store a message id/hash in `PendingOrder` at commit time (mirroring `MessageAccepted { id, nonce }`) and require `receipt.topic == order.id` (or equivalent) before paying the reward and removing the order, exactly as the external report recommends including the full committed data (not just a subset) in the value that gates acceptance.

### Proof of Concept
Not able to construct a concrete end-to-end PoC from the Rust code alone, because the actual `topic` field's semantics and how the Ethereum Gateway contract derives/emits it are defined outside this repository (Solidity contracts), and I could not verify within the indexed content whether any other check elsewhere (e.g., in `Verifier::verify` or upstream event structure) already enforces the missing binding. This is flagged as an uncertain/unconfirmed analog rather than a fully proven local vulnerability.