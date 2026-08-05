Audit Report

## Title
`Nfts::transfer` Fails to Clear `ItemAttributesApprovalsOf`, Letting a Former Owner's Delegate Retain Write Access After Ownership Transfer - (File: `substrate/frame/nfts/src/features/transfer.rs`)

## Summary
`Pallet::do_transfer` updates `Account`/`Item.owner` and purges `details.approvals`, `ItemPriceOf`, and `PendingSwapOf` on every item transfer, but never clears `ItemAttributesApprovalsOf` for the transferred item. Because attribute-write authority in the `Account(delegate)` namespace is validated solely against this stale map (not against the item's current owner), a delegate approved by the previous owner keeps owner-granted write access to the item's attributes after the item has been sold/transferred to a new, non-consenting owner.

## Finding Description
`do_transfer` in `substrate/frame/nfts/src/features/transfer.rs` explicitly resets `details.approvals`, `ItemPriceOf`, and `PendingSwapOf` but never touches `ItemAttributesApprovalsOf`:
<cite repo="Kohvert/polkadot-sdk--037" path="substrate/frame/nfts/src/features/transfer.rs" start="89-103" />

`ItemAttributesApprovalsOf` is populated only via `do_approve_item_attributes`, which checks `check_origin == details.owner` at approval time:
<cite repo="Kohvert/polkadot-sdk--037" path="substrate/frame/nfts/src/features/attributes.rs" start="382-393" />

Later attribute writes to the `Account(delegate)` namespace are authorized purely by membership in `ItemAttributesApprovalsOf`, independent of the item's *current* owner, in `is_valid_namespace`:
<cite repo="Kohvert/polkadot-sdk--037" path="substrate/frame/nfts/src/features/attributes.rs" start="474-479" />

Since `transfer`, `buy_item`, and `do_claim_swap` all funnel through `do_transfer`, and `do_transfer` never removes entries from `ItemAttributesApprovalsOf`, a delegate approved by the old owner remains a valid attribute-setter for the item's `Account(delegate)` namespace after the item changes hands. Only an explicit `do_cancel_item_attributes_approval` call by the current owner (`substrate/frame/nfts/src/features/attributes.rs` lines 412-453) removes the stale approval — and the new owner has no visibility into which delegate(s) were approved by the previous owner, since the `Transferred` event does not surface this state.

## Impact Explanation
This is a permissionless-entrypoint state-desync bug: `transfer`/`buy_item`/`do_claim_swap` are ordinary signed dispatchables, and a successful call silently leaves a third party (the previous owner's delegate) with continued owner-granted write authority (`set_attribute` in the `Account(delegate)` namespace) over an item now owned by someone else. This lets an ex-owner (via a delegate account they control) continue to mutate on-chain attribute state tied to an NFT they no longer own, without the new owner's knowledge or consent — corrupting the intended invariant that `transfer` hands exclusive control of the item, including all owner-granted permissions, to the new owner. This matches the "runtime bugs that compromise intended behavior" / origin-widening class of accepted impact, since it is effectively a residual owner-scoped write capability that survives an ownership change and was never re-authorized by the new owner.

## Likelihood Explanation
Fully reachable by an ordinary unprivileged account: mint or acquire an item, call `approve_item_attributes` for a delegate, transfer/sell the item via `transfer`, `buy_item`, or a swap-claim, and the delegate retains write access via `set_attribute` using `AttributeNamespace::Account(delegate)`. No governance, validator, or privileged role is required; only the `Attributes` and `Trading`/`Transfer` pallet features need to be enabled, which is the case on shipped asset-hub configurations.

## Recommendation
In `do_transfer` (and any other code path that reassigns `Item.owner`, e.g. `do_claim_swap`), also drain/clear `ItemAttributesApprovalsOf` for the `(collection, item)` pair, mirroring the existing handling of `details.approvals`, `ItemPriceOf`, and `PendingSwapOf`, and unreserve/settle any deposits tied to those approvals as `do_cancel_item_attributes_approval` does. This atomically synchronizes all owner-scoped mappings with the ownership change.

## Proof of Concept
1. Owner A mints item `(collection, item)` and calls `approve_item_attributes(collection, item, delegate)`, populating `ItemAttributesApprovalsOf[(collection, item)]` with `delegate`.
2. `delegate` calls `set_attribute(collection, item, AttributeNamespace::Account(delegate), key, value)`, succeeding because `is_valid_namespace` passes.
3. A transfers/sells the item to B via `Nfts::transfer` (or `buy_item`). `do_transfer` updates `Item.owner` to B and clears `details.approvals`, `ItemPriceOf`, `PendingSwapOf`, but leaves `ItemAttributesApprovalsOf[(collection, item)]` containing `delegate`.
4. `delegate` calls `set_attribute(collection, item, AttributeNamespace::Account(delegate), key2, value2)` again. `is_valid_namespace` still returns true because `approvals.contains(&origin)` remains true, even though B never approved `delegate` and is now the sole owner.
5. B has no direct way to discover or revoke this without knowing the exact `delegate` address A had approved, confirming the stale, invisible owner-scoped permission survives the ownership transfer.