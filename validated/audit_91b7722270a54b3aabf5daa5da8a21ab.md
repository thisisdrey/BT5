### Title
Wnear `storage_deposit` amount permanently lost when `near_withdraw` fails during `nft_withdraw` - ([File: contracts/defuse/src/contract/tokens/nep171/withdraw.rs])

### Summary
`internal_nft_withdraw` synchronously debits the owner's Verifier balance for both the NFT (`Nep171TokenId`) and, when `storage_deposit` is set, for the wnear amount (`Nep141TokenId`) in a single `Contract::withdraw` call before any promise executes. If the chained `near_withdraw` call fails, `do_nft_withdraw`'s `require!(promise_result_checked_void(0).is_ok(), ...)` panics, aborting the storage-deposit/transfer promise, but the unconditionally-scheduled `nft_resolve_withdraw` callback only ever re-credits the `Nep171TokenId` (the NFT); it has no code path to refund the `Nep141TokenId` wnear amount, so that amount is permanently burned from the Verifier's books with nothing delivered to anyone.

### Finding Description
Binding that must hold: `(wnear balance debited = X) == (assets delivered to token/receiver = 0) + (amount re-credited to owner_id = 0)`. This is violated because re-credit is always 0 for wnear on this path.

Code path:
1. `internal_nft_withdraw` (contracts/defuse/src/contract/tokens/nep171/withdraw.rs:64-78) calls `self.withdraw(&owner_id, [(Nep171TokenId, 1), (Nep141TokenId(wnear), X)], ...)`, which synchronously subtracts both balances from the owner's account via `Contract::withdraw` (contracts/defuse/src/contract/tokens/mod.rs:76-128) — this happens before any cross-contract call resolves.
2. It then schedules `ext_wnear::near_withdraw(X)` chained `.then(do_nft_withdraw)` chained `.then(nft_resolve_withdraw(token, owner_id, token_id, is_call))` (lines 82-108).
3. In `do_nft_withdraw` (lines 121-138), `require!(promise_result_checked_void(0).is_ok(), "near_withdraw failed")` panics if `near_withdraw` failed. This means the `storage_deposit` and `nft_transfer`/`nft_transfer_call` promises are never created.
4. `nft_resolve_withdraw` (lines 160-195) still runs regardless (NEAR always invokes `.then()` callbacks even after ancestor failure), and calls `promise_result_checked_void(0)` / `promise_result_checked_json(0)` against the (failed) `do_nft_withdraw` promise result. On failure it re-credits **only** `Nep171TokenId::new(token, token_id)` for 1 unit (line 185-190) — there is no reference anywhere in this callback to `Nep141TokenId`/wnear, so the X amount debited in step 1 is never restored.

Attacker's exact call: any owner calls `nft_withdraw`/`nft_force_withdraw` (or triggers via `execute_intents` with an `NftWithdraw` intent) with `storage_deposit = Some(X)` for an NFT they own, targeting a `token` contract whose `storage_deposit` requires more attached deposit/balance than `near_withdraw(X)` will actually provide, or simply arranging for the wnear contract's `near_withdraw` to fail (e.g., insufficient wnear balance backing the withdrawal, wnear contract paused, or gas exhaustion on that specific cross-contract call). Since `near_withdraw` is a real external call to the wnear contract, an owner can engineer failure conditions (e.g., by withdrawing/burning their own wnear balance out from under the contract's internal accounting via other combined operations in the same flow, or simply relying on transient failure/gas conditions) to trigger the `require!` panic deterministically for their own withdrawal.

No existing guard prevents this: `promise_result_checked_void` only detects success/failure, it doesn't drive any refund of the second `TokenId`; `nft_resolve_withdraw`'s refund logic is hard-coded to only handle `Nep171TokenId`.

### Impact Explanation
The `owner_id` who called `nft_withdraw` loses `X` yoctoNEAR-equivalent of wnear balance from their Verifier account permanently — no promise delivers it externally (near_withdraw failed) and no resolver code path re-credits it internally. This is a genuine "user funds permanently frozen/lost" case matching the Critical impact category (value leaving/being destroyed from the Verifier's custodied balance without ever being delivered or refunded). It's repeatable by the same or any account for every `nft_withdraw`/`nft_force_withdraw` call using `storage_deposit`, so the blast radius scales with how often withdrawals combine NFT transfer with wnear-based storage deposit funding.

### Likelihood Explanation
Precondition: the withdrawing owner must hold sufficient wnear-backed balance in the Verifier to cover `storage_deposit` and must own the NFT being withdrawn — both are attacker-controlled (self-funded) conditions, no privileged role needed. Triggering `near_withdraw` failure requires either an adverse but plausible external condition (target FT/NFT storage_deposit misconfiguration, wnear contract being paused, or attacker manipulating amounts to make `near_withdraw` revert, e.g. requesting more than the wnear contract's registered balance for the Defuse contract) — these are realistic failure modes for a real cross-contract call, not purely theoretical, and the loss is entirely the withdrawer's own funds (self-inflicted but still a protocol accounting bug: balance debited from Verifier's books is not restored, which breaks the "assets delivered + amount re-credited = balance debited" invariant). This mirrors the acknowledged sibling case in `ft_withdraw`.

### Recommendation
In `nft_resolve_withdraw` (and correspondingly `ft_resolve_withdraw`), when `storage_deposit` was used and the underlying `do_nft_withdraw`/`do_ft_withdraw` promise failed (i.e., `near_withdraw` or the subsequent transfer never happened), also refund the `Nep141TokenId(wnear)` amount that was debited for `storage_deposit`, not just the NFT/FT token. This requires plumbing the `storage_deposit` amount through to `nft_resolve_withdraw`/`ft_resolve_withdraw` (currently not passed) and adding a `self.deposit(sender_id, [(Nep141TokenId(wnear), storage_deposit)], ...)` call whenever the do_*_withdraw promise result indicates failure before the transfer was attempted.

### Proof of Concept
```
// tests/src/tests/defuse/intents/nft_withdraw.rs (new sandbox test)
// 1. Register owner, fund Verifier balance with wnear (Nep141TokenId) = X and an NFT (Nep171TokenId).
// 2. Deploy a wnear mock/real wnear contract configured so that `near_withdraw(X)` fails
//    (e.g., contract's registered balance for defuse < X, or pause the wnear contract's near_withdraw method).
// 3. Call nft_withdraw(token, receiver_id, token_id, memo=None, msg=None) via
//    Contract::internal_nft_withdraw path with storage_deposit = Some(X) (via nft_force_withdraw
//    with UnrestrictedWithdrawer role, or via an intent path that sets storage_deposit)
//    — attach 1 yoctoNEAR as required.
// 4. Assert do_nft_withdraw promise fails (near_withdraw failed) and nft_resolve_withdraw returns `false`.
// 5. Assert: owner's Verifier balance for Nep171TokenId(token, token_id) == 1 (refunded - NFT restored).
// 6. Assert: owner's Verifier balance for Nep141TokenId(wnear) == 0, i.e. NOT refunded,
//    while total_supplies for wnear TokenId was decremented by X and never re-added —
//    proving X is permanently lost: no promise delivered wnear externally, and no internal
//    balance restores it.
```