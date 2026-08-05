Confirmed: no reference to `success` anywhere in the `outbound-queue-v2` pallet implementation, only in the test fixture at `snowbridge_v2_outbound.rs` where it's hardcoded to `true`. This confirms `process_delivery_receipt` never reads or branches on `receipt.success`.

Audit Report

## Title
Relayer reward and message-delivery state are settled unconditionally, ignoring the `success` flag decoded from the Ethereum delivery receipt - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt::try_from` in `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` decodes the Ethereum `InboundMessageDispatched` event, including its `success: bool` field, but `Pallet::process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` never reads `receipt.success` before paying the relayer reward and removing the `PendingOrder`. This causes relayers to be rewarded, and the pending-order tracking state to be irrecoverably deleted, even when the dispatched message reverted/failed on Ethereum.

## Finding Description
The `InboundMessageDispatched` Solidity event explicitly carries a `success` boolean describing the on-chain execution outcome on Ethereum:
<cite repo="Lauraivanka/polkadot-sdk--034" path="bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs" start="10-27" />

`DeliveryReceipt::try_from` decodes this field faithfully into `receipt.success`:
<cite repo="Lauraivanka/polkadot-sdk--034" path="bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs" start="38-51" />

`process_delivery_receipt`, however, only checks `receipt.gateway` and the `PendingOrders` lookup by `nonce`. It never inspects `receipt.success`; it pays `order.fee` to the reward account, unconditionally removes the pending order, and emits `MessageDelivered` regardless of whether the dispatch on Ethereum actually succeeded:
<cite repo="Lauraivanka/polkadot-sdk--034" path="bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs" start="445-480" />

The `PendingOrder` struct, which is the only on-chain state tracking a message awaiting delivery/retry, has no field to record delivery outcome — it only stores `nonce`, `block_number`, and `fee`:
<cite repo="Lauraivanka/polkadot-sdk--034" path="bridges/snowbridge/pallets/outbound-queue-v2/src/types.rs" start="14-24" />

A grep of the pallet's implementation confirms `success` is never referenced anywhere in `bridges/snowbridge/pallets/outbound-queue-v2/`, only appearing hardcoded to `true` in integration test fixtures (e.g. `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs:409-415`), confirming the production code path never branches on this field.

## Impact Explanation
Once a relayer submits a valid Merkle/receipt proof for *any* `InboundMessageDispatched` event with a correct nonce and gateway address — regardless of `success` being `true` or `false` — `process_delivery_receipt` pays the full `order.fee` via `T::RewardPayment::register_reward` and unconditionally calls `<PendingOrders<T>>::remove(nonce)`. This produces two concrete corrupted-state outcomes:
1. Relayers are paid `order.fee` (the reward amount) for deliveries that reverted on Ethereum — an underpriced/incorrect payout not tied to actual completed work.
2. The `PendingOrders` entry for that nonce — the only state tracking that a message still needs delivery/retry — is permanently deleted even on failure, which is a permanent loss of bridge state for that nonce, with no path left in this pallet to detect or retry the failed dispatch.

This matches the required impact category of "public underpriced work that degrades... stalls bridge processing" combined with "permanent... bridge-state lock" (here, an unrecoverable state loss rather than a lock, but functionally equivalent in effect: the tracking record needed to resolve/react to the failure is gone forever), and it is a case of message/receipt state advancing without the atomic success check the pivots require ("Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically").

## Likelihood Explanation
Any unprivileged relayer holding a valid Merkle/receipt proof for a real `InboundMessageDispatched` log — which the Ethereum gateway emits regardless of whether the dispatched commands succeeded — can call the public extrinsic `submit_delivery_receipt` and trigger this unconditional reward/removal path. No malicious relayer collusion, privileged access, or off-chain infrastructure control is required beyond simply obtaining a receipt for a message that failed on Ethereum, which is a normal occurrence (e.g., out-of-gas or reverted commands).

## Recommendation
Store and check `receipt.success` in `process_delivery_receipt` before crediting the reward and removing the order:
- Only call `T::RewardPayment::register_reward` and remove the `PendingOrder` / emit `MessageDelivered` when `receipt.success == true`.
- On `receipt.success == false`, emit a distinct event (e.g. `MessageDeliveryFailed`) and either retain the `PendingOrder` for retry/resubmission or route it through an explicit failure-handling path instead of silently treating it as delivered.

## Proof of Concept
1. A message with nonce `N` and fee `F` is committed via `do_process_message`, creating `PendingOrders[N] = { nonce: N, fee: F, block_number }` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:426-440`).
2. The relayer relays the message to the Ethereum gateway; execution of the dispatched commands reverts, so the gateway emits `InboundMessageDispatched(nonce=N, topic, success=false, reward_address)`.
3. The relayer obtains a valid Merkle/receipt proof for this event and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` correctly decodes `success: false`, but `process_delivery_receipt` never reads this field (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:445-480`).
5. `order.fee` (`F`) is paid to the relayer via `register_reward`, `PendingOrders[N]` is removed, and `Event::MessageDelivered { nonce: N }` is emitted — identical to the successful path, even though the dispatch failed on Ethereum. This can be reproduced as a unit test analogous to the existing `invalid_nonce_for_delivery_receipt_fails` test in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs:949-969`, but constructing a `DeliveryReceipt { success: false, .. }` and asserting `process_delivery_receipt` still succeeds and pays the reward.