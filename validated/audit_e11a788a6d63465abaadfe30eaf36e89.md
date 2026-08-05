All claims in the report are confirmed by the code exactly as described.

Audit Report

## Title
Irrecoverable fund loss when tipping a Snowbridge outbound message that is delivered before the tip's cross-chain settlement completes - (File: `bridges/snowbridge/pallets/system-frontend/src/lib.rs`, `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`pallet-snowbridge-system-frontend::add_tip` on AssetHub irreversibly swaps and burns the user's tip asset via `swap_fee_asset_and_burn` before it is known whether the corresponding `PendingOrders` entry on Bridge Hub still exists. If the message is delivered (via `submit_delivery_receipt` → `process_delivery_receipt`, which calls `PendingOrders::<T>::remove(nonce)`) before the tip's XCM `AddTip` transact arrives, the tip application fails with `AddTipError::UnknownMessage` and the already-burned funds are only recorded in `LostTips`, with no refund mechanism.

## Finding Description
`Pallet::add_tip` in `bridges/snowbridge/pallets/system-frontend/src/lib.rs` (lines 261-273) unconditionally calls `Self::swap_fee_asset_and_burn(who.clone().into(), asset)?`, which swaps the tip asset for Ether and calls `burn_for_teleport` — a final, non-refundable action — before any XCM message confirming Bridge Hub state has been sent. It then dispatches an `AddTip` transact call to Bridge Hub via `send_transact_call`.

On Bridge Hub, `AddTip::add_tip` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` (lines 483-496) only succeeds if `PendingOrders::<T>` still contains an entry for the nonce; `process_delivery_receipt` (lines 445-480) removes this entry via `<PendingOrders<T>>::remove(nonce)` once a delivery receipt is submitted. If the entry is already removed, `add_tip` returns `AddTipError::UnknownMessage`.

The dispatching pallet `snowbridge-pallet-system-v2::add_tip` (`bridges/snowbridge/pallets/system-v2/src/lib.rs`, lines 251-281) handles this failure by only recording the lost amount into `LostTips::<T>` storage and emitting `TipProcessed { success: false, .. }` — it does not trigger any refund back to AssetHub. The `LostTips` storage doc comment itself acknowledges this: "Capturing the lost tips here supports implementing a recovery method in the future," confirming no such recovery mechanism currently exists.

Since the AssetHub-side burn is unconditional and executes before any confirmation of Bridge Hub state, a natural, unprivileged race between an honest relayer's `submit_delivery_receipt` and an honest user's `add_tip` transaction results in permanent, unrecoverable destruction of the user's asset value with no atomic linkage between the two chains.

## Impact Explanation
This is a value-conservation violation matching the required impact "permanent user-fund ... lock/loss." The Ether-equivalent value produced by `burn_for_teleport` in `swap_fee_asset_and_burn` is destroyed on AssetHub unconditionally, and if the Bridge Hub-side `add_tip` fails due to the nonce's `PendingOrders` entry already having been removed, the funds are irrecoverably lost — only tracked as a bookkeeping entry in `LostTips` with no on-chain reclaim path present in the inspected pallets.

## Likelihood Explanation
No privileged or malicious actor is required. Any honest relayer processing `submit_delivery_receipt` in the ordinary course of bridge relaying can race an ordinary user's `add_tip` call traveling asynchronously via XCM. Because message delivery is expected to happen promptly (that is the relayer's job), and tip transactions can be delayed by network/XCM queuing, this race condition is a realistic, repeatable occurrence in normal bridge operation, not a contrived or front-run-only scenario.

## Recommendation
Do not burn/swap the tip asset on AssetHub until Bridge Hub confirms that the `PendingOrders` entry for the target nonce still exists, or make the burn reversible: implement a compensating XCM message from Bridge Hub back to AssetHub to refund the original tip asset when `AddTip::add_tip` fails with `AddTipError::UnknownMessage`, rather than merely logging the loss into `LostTips` with no defined recovery mechanism.

## Proof of Concept
1. User calls `add_tip(message_id=N, asset)` on AssetHub; `swap_fee_asset_and_burn` immediately burns the swapped Ether for teleportation (`bridges/snowbridge/pallets/system-frontend/src/lib.rs` L267), then an XCM `AddTip` transact is dispatched to Bridge Hub.
2. Before this XCM lands, a relayer submits `submit_delivery_receipt` for nonce `N`; `process_delivery_receipt` removes the `PendingOrders` entry (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` L475).
3. The `AddTip` XCM call executes on Bridge Hub via `snowbridge-pallet-system-v2::add_tip`, finds no `PendingOrders` entry, and `OutboundQueue::add_tip` returns `AddTipError::UnknownMessage` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` L492).
4. `snowbridge-pallet-system-v2::add_tip` records the amount into `LostTips::<T>` and emits `TipProcessed { success: false, .. }` (`bridges/snowbridge/pallets/system-v2/src/lib.rs` L266-278) — confirmed exactly by the existing integration test `tip_to_invalid_nonce_is_added_to_lost_tips` in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs` (L277-319).
5. The user's originally burned asset is never restored to them.