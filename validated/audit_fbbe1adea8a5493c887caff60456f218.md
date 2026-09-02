### Title
Protocol fees bypassed via structuring `TokenDiff` intents into amount=1 chunks on `Nep245`/`Imt` tokens - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` exempts any `Nep245`/`Imt` negative delta with `amount <= 1` from fees, and this exemption is evaluated per-intent rather than per aggregate trade. An unprivileged signer can split a single large `TokenDiff{token_id: -N}` into `N` separate signed `TokenDiff{token_id: -1}` intents inside one `MultiPayload`/`execute_intents` batch, each independently qualifying for `Pips::ZERO`, so the total `fees_collected` credited to `fee_collector` is `0` instead of `Pips::fee_ceil(N)`.

### Finding Description
The broken binding is:
`fee_owed(trade moving N units of Nep245 token via one TokenDiff{delta=-N}) == fee_owed(same N units moved via N separate TokenDiff{delta=-1} intents by the same signer in one batch)`

In `TokenDiff::execute_intent` (contracts/defuse/core/src/intents/token_diff.rs:59-79), for each `(token_id, delta)` pair in a single intent's diff, if `delta < 0` the fee is computed as:
```
let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
```
`Self::token_fee` (lines 206-216) is:
```rust
pub fn token_fee(token_id: impl Into<TokenIdType>, amount: u128, fee: Pips) -> Pips {
    let token_id = token_id.into();
    match token_id {
        TokenIdType::Nep141 => {}
        TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
        // do not take fees on NFTs and MTs with |delta| <= 1
        TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
    }
    fee
}
```
The exemption is keyed solely on the `amount` field passed by the *current intent*, not on any cumulative or account-level state, and not on whether the underlying `Nep245` token id actually represents a unique, non-fungible unit (total supply 1) versus a divisible balance with supply `>> 1`.

Because `TokenDiff` intents are executed independently — each is its own signed `DefusePayload` intent, verified/nonce-checked individually, and `execute_intent` applies the delta and computes the fee purely from that single intent's `amount` — a signer who wants to move `N` units (`N>1`) of the same `Nep245TokenId` can instead sign `N` intents each with `delta = -1` on that token id, packed into the same `MultiPayload` sent to `execute_intents`. Each of the `N` intents independently satisfies `amount <= 1`, so `token_fee` returns `Pips::ZERO` on every one of them, and `fees_collected` for that token across the whole batch sums to `0`.

Contrast with a single intent moving the same cumulative amount: `token_fee` would see `amount = N > 1`, fall through to `fee`, and `fee_ceil(N)` would be `> 0` for any nonzero protocol fee. The net real value transferred out of the signer's account is identical in both cases (their balance decreases by `N` units of the token), and the `TransferMatcher`/`finalize` netting (contracts/defuse/core/src/engine/state/deltas.rs:265-283, 337-391) only requires that deposits and withdrawals across the whole batch net to zero per token — it enforces no relationship between fee owed and total transferred value, and does not re-derive or validate fee based on aggregated deltas per token/signer. `MultiPayload::verify`, nonce checks (`commit_nonce`/`is_nonce_used`), and `assert_one_yocto` are all satisfied normally since each intent is independently, validly signed by the attacker's own key with a fresh nonce — none of these guards address fee correctness, only signature/replay validity.

The attacker's exact payload: one `MultiPayload` containing `N` `DefusePayload`s, each wrapping a single `TokenDiff` intent with `diff = {Nep245TokenId(contract, token_id): -1, <counter-token>: +k}`, all signed by the attacker's own key with distinct nonces, submitted via `execute_intents`.

### Impact Explanation
The `fee_collector` receives strictly less (`0` vs `fee_ceil(N) > 0`) of the `Nep245`/`Imt` token that the attacker gives up in the trade, for an economically identical transfer of value. This is repeatable indefinitely by any signer trading `Nep245`/`Imt` tokens (any multi-token contract where a given `token_id` supports balances `> 1`), across any batch size, with no cost beyond ordinary gas and normal signing — it does not require any privileged role, relayer key, or victim key. This matches the Critical category "protocol fees bypassed" since it is fee under-collection driven purely by how the attacker chooses to structure otherwise-identical, fully self-authorized intents.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs to be a normal Verifier user holding (or trading into) a `Nep245`/`Imt`-typed balance with amount `> 1`, and the ability to sign multiple `TokenDiff` intents and batch them in one `MultiPayload`/`execute_intents` call — both are ordinary, unprivileged capabilities explicitly granted to the attacker profile in scope. The only cost is proportional gas for `N` intents instead of `1`, which is economically trivial relative to the fee saved on any moderately large or high-fee trade. This is fully repeatable across tokens, accounts, and batches.

### Recommendation
Base the NFT/MT fee exemption on the intrinsic nature of the token id (e.g., whether the specific `Nep245`/`Imt` `token_id` is registered/known to have `max_supply == 1`, i.e., is truly non-fungible) rather than on the transient `amount` of an individual intent's delta. Alternatively, aggregate all `TokenDiff` deltas per `(signer, token_id)` across the entire batch before applying `token_fee`, so that splitting one logical transfer into multiple intents cannot change the amount used for the fee-exemption decision.

### Proof of Concept
```rust
// contracts/defuse/core/src/intents/token_diff.rs (new #[cfg(test)] case)
#[test]
fn structuring_bypasses_fee_on_nep245() {
    let token_id: TokenId = Nep245TokenId::new(
        "mt.near".parse().unwrap(), "ft1".to_string(),
    ).into();
    let fee = Pips::ONE_PERCENT; // nonzero protocol fee
    let n: u128 = 50;

    // Side A: one TokenDiff intent with delta = -N
    let fee_single = TokenDiff::token_fee(token_id.clone(), n, fee).fee_ceil(n);
    assert!(fee_single > 0, "single large transfer should pay nonzero fee");

    // Side B: N TokenDiff intents each with delta = -1 on the same token_id
    let fee_split: u128 = (0..n)
        .map(|_| TokenDiff::token_fee(token_id.clone(), 1, fee).fee_ceil(1))
        .sum();
    assert_eq!(fee_split, 0, "structured transfer pays zero fee");

    // The binding fee(single delta=-N) == fee(N split delta=-1 each) is violated:
    assert_ne!(fee_single, fee_split);
}
```
This can be extended to a full `near-workspaces` sandbox test executing two `MultiPayload`s via `execute_intents` — one with a single `TokenDiff{Nep245TokenId: -50}` intent and one with 50 separate signed `TokenDiff{Nep245TokenId: -1}` intents — and asserting that `fee_collector`'s balance increases in the first case but not in the second, despite identical net token movement from the signer's account.