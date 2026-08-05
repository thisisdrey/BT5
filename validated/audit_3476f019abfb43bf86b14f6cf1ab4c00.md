Confirmed the code matches the claim exactly: `process_delivery_receipt` at bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs L445-480 only checks `receipt.gateway` and pending order existence, pays `order.fee` via `T::RewardPayment::register_reward`, and unconditionally removes the `PendingOrder`, never inspecting `receipt.success`. The `DeliveryReceipt` struct at bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs L16-27 does carry a `success: bool` field decoded from the real Ethereum `InboundMessageDispatched` event, confirming this field exists and is available but unused for gating payout/removal.

Audit Report

## Title
`snowbridge-pallet-outbound-queue-v2::process_delivery_receipt` pays relayer reward and settles `PendingOrder` without checking the `success` field of the delivery receipt - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
`process_delivery_receipt` pays the relayer's `order.fee` reward and permanently removes the corresponding `PendingOrder` on receipt of any verified `DeliveryReceipt` for a known nonce, without ever checking `receipt.success`. A legitimately verified receipt reporting `success: false` (an honest, real failure of message execution on the Ethereum Gateway) is treated identically to a successful delivery, causing underpriced payout for non-delivery and permanent, unrecoverable loss of the accounting/retry state for that message.

## Finding Description
`submit_delivery_receipt` (L298-317) verifies the Merkle/event proof via `T::Verifier::verify`, decodes it into a `DeliveryReceipt` via `DeliveryReceipt::try_from`, and calls `Self::process_delivery_receipt(relayer, receipt)`. The `DeliveryReceipt` type, decoded from the real `InboundMessageDispatched` Ethereum event (bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs L10-27, L38-51), carries a `success: bool` field taken directly from the on-chain Ethereum event.

`process_delivery_receipt` (L445-480) only:
1. Checks `T::GatewayAddress::get() == receipt.gateway`.
2. Resolves `reward_account`.
3. Fetches `order = PendingOrders::<T>::get(nonce)`.
4. If `order.fee > 0`, unconditionally calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)`.
5. Unconditionally removes the order via `<PendingOrders<T>>::remove(nonce)`.

At no point does this function read or branch on `receipt.success`. The only existing guard shown in the test suite (`poc_m1` in `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs` L390-416) blocks payout only when the *verifier* itself reports the bridge halted — a completely different condition from a legitimately verified event reporting `success: false`. There is no code path in `process_delivery_receipt` that distinguishes a failed on-Ethereum execution from a successful one; both pay the reward and delete the `PendingOrder`.

## Impact Explanation
This directly violates the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Any relayer who submits a legitimately verifiable receipt for a message whose execution reverted on the Ethereum Gateway (`success: false`) still collects the full `order.fee` reward via `T::RewardPayment::register_reward`, and the corresponding `PendingOrder` (keyed by `nonce`) is deleted forever with no retry or distinct failure-handling path. This is duplicate/incorrect settlement and public underpriced work that drains the bridge's reward funds without a corresponding successful delivery, and it permanently destroys the pending-order accounting state needed to re-attempt or correctly settle that message.

## Likelihood Explanation
High. `submit_delivery_receipt` is a public, unprivileged extrinsic gated only by `ensure_signed` (L307). No governance, admin, or malicious-validator assumption is needed — an attacker/relayer only needs a legitimate Ethereum event log reporting `success: false` (which can occur organically, e.g., any message whose Gateway-side execution reverts, or be induced deliberately) and a valid Merkle/event proof for it, both of which are honest, verifiable data. This is repeatable across every message with `fee > 0` that fails on Ethereum.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- If `true`, keep current behavior (pay reward, remove `PendingOrder`).
- If `false`, do not call `T::RewardPayment::register_reward` from this path; either retain the `PendingOrder` for a legitimate retry/settlement flow or route it to an explicit "failed delivery" handling path (e.g., a distinct event and a defined refund/retry/slash policy), ensuring unsuccessful execution never triggers payout or unrecoverable removal of pending accounting state.

## Proof of Concept
1. A message is enqueued via `do_process_message` (L343-443), inserting `PendingOrders::<T>::insert(nonce, PendingOrder { nonce, fee, block_number })` with `fee > 0` (L426-436).
2. The message is delivered to the Ethereum Gateway, but its execution reverts; the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer calls `submit_delivery_receipt` with a valid Merkle/event proof for this event; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false`.
4. `process_delivery_receipt` (L445-480) proceeds without checking `receipt.success`: it pays `order.fee` to `reward_account` via `T::RewardPayment::register_reward` and removes the order via `<PendingOrders<T>>::remove(nonce)`, emitting `Event::MessageDelivered`.
5. A unit test can assert: after inserting a `PendingOrder` with `fee > 0` and calling `process_delivery_receipt` with a receipt where `success == false`, `RewardPayment::register_reward` is still invoked and `PendingOrders::<T>::get(nonce)` returns `None` — demonstrating payout and irreversible removal despite failed delivery.