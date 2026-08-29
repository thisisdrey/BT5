### Title
Same-public-key DeleteKey+AddKey resets nonce backward, enabling in-block transaction replay - ([File: runtime/runtime/src/access_keys.rs])

### Finding Description
`initial_nonce_value(block_height)` returns `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` (1e6) and is used unconditionally by `add_regular_key` (`runtime/runtime/src/access_keys.rs:230-255`, seeding at line 240) whenever an `AddKey` action is applied — regardless of whether the same public key previously existed on the account and had already advanced its nonce higher within the *current* block. `action_add_key` (`:149-192`) only rejects re-adding a key that currently exists (`AddKeyAlreadyExists`, `:157`); it performs no check for a key that was *just deleted earlier in the same transaction/chunk* to preserve its prior nonce.

`docs/DataStructures/AccessKey.md` documents the intended invariant explicitly: "If the new access key reuses the same public key, the nonce of the new access key should be equal to the nonce of the old access key. It's required to avoid replaying old transactions again." The implementation does not honor this invariant; it always reseeds to the block-height-derived value.

Exploit flow within a single block `h` (the only block height for which `(h-1)*1e6` can fall below nonces already consumed in that same block, since nonces are bounded to `< h*1e6` by `verify_nonce`'s upper-bound check, `runtime/runtime/src/verifier.rs:229-235`):
1. Attacker's FullAccess key signs and gets applied N transactions with nonces `(h-1)*1e6 + 1 … (h-1)*1e6 + k`, advancing `access_key.nonce` to `(h-1)*1e6 + k`.
2. Attacker submits one more transaction (nonce `(h-1)*1e6+k+1`, still valid) containing actions `[DeleteKey(pk), AddKey(pk, …)]`. `action_delete_key` removes the key (`:52-91`), then `action_add_key`→`add_regular_key` re-adds it with `access_key.nonce = initial_nonce_value(h) = (h-1)*1e6` (`:240`) — lower than the nonce already reached in step 1.
3. Attacker resubmits (or the network re-includes) a previously-signed, already-executed transaction whose nonce lies in `[(h-1)*1e6+1, (h-1)*1e6+k]`.
4. `verify_nonce` (`runtime/runtime/src/verifier.rs:211-237`, `Monotonic` branch `:218-222`) only checks `tx_nonce > current_nonce`; since `current_nonce` is now `(h-1)*1e6` (lower than the replayed tx's nonce), the check passes and the already-executed transaction is re-validated and re-applied, double-executing its actions (e.g., a `Transfer`).

### Impact Explanation
This is a genuine break of the nonce-based replay-protection invariant explicitly documented for access-key recreation. It allows an already-executed, previously-consumed transaction to be re-validated and re-applied within the attacker's own account, enabling a double-spend/replay of that transaction's effects (e.g., doubling a transfer's effect on the account's own state, or duplicating any other action the replayed transaction performed). This falls under the "double-spend/replay" bounty category as scoped.

### Likelihood Explanation
Preconditions are attacker-controlled and cheap: a funded account with one FullAccess key, and the ability to get several of its own transactions (increment, delete+add combo, and a replay) included within a single chunk at the same block height. Per `test-loop-tests/src/tests/pending_transaction_queue.rs`, non-contract accounts face no `P_MAX`-style restriction on the number of transactions from one account per chunk, so an attacker's own account can legitimately have multiple transactions processed within one chunk. The critical constraint is that all of this — including the resubmitted/duplicated old transaction — must land in the *same* chunk at height `h`, since only within that block does `(h-1)*1e6` fall below nonces already used at that height. Getting a byte-identical resubmission accepted a second time in the same chunk depends on client/pool/gossip behavior (duplicate-hash handling) that was not fully verified in this review — this is the primary source of uncertainty in confirming practical exploitability end-to-end, though nothing in the reviewed nonce/access-key logic itself blocks it.

### Recommendation
When re-adding a regular access key with a public key that matches a key deleted earlier in the same block/chunk apply (or more robustly, always), do not blindly reseed to `initial_nonce_value(block_height)`. Instead, track and reuse the maximum nonce previously observed for that `(account_id, public_key)` pair within the same apply/chunk (mirroring the documented invariant of preserving the old key's nonce), or take `max(initial_nonce_value(block_height), highest_nonce_seen_this_block_for_this_key)` when seeding a re-added key's nonce.

### Proof of Concept
Runtime apply-path integration test (in `runtime/runtime/src/tests/apply.rs` or `integration-tests`):
1. Fund `alice.near` with one FullAccess key `pk`.
2. At block height `h`, submit and apply, in order, within the same chunk:
   - `Transfer` tx #1 with nonce `(h-1)*1e6+1`, moving funds to `bob.near`.
   - Combo tx with nonce `(h-1)*1e6+2` containing `[DeleteKey(pk), AddKey(pk, FullAccess)]`.
   - A resubmission of the exact same signed bytes as tx #1 (nonce `(h-1)*1e6+1`).
3. Assert: the resubmitted tx is accepted (not rejected with `InvalidNonce`), and `bob.near`'s balance reflects the transfer amount applied twice, and `alice.near`'s balance is debited twice — demonstrating double-spend via nonce rollback triggered by `add_regular_key`'s use of `initial_nonce_value`.