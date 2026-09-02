## Title
Fee bypass on `Nep245`/`Imt` legs via decomposition of a single M-magnitude `TokenDiff` into M unit-magnitude `TokenDiff` intents - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

## Summary
`TokenDiff::execute_intent` computes the protocol fee per intent, calling `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` where `amount` is the magnitude of that single intent's delta on a given token. For `Nep245`/`Imt` token types, `token_fee` returns `Pips::ZERO` whenever `amount <= 1`. Because a signer can bundle an arbitrary number of `TokenDiff` intents inside one signed `DefuseIntents` payload, they can split one intended magnitude-M leg into M separate unit (`|delta| == 1`) `TokenDiff` intents on the same `TokenId`, causing the fee-exemption branch to trigger for each of them and reducing the total protocol fee collected on that leg to `0` instead of `Pips::fee_ceil(protocol_fee, M)`.

## Finding Description
The binding that should hold is: for a signer's net negative exposure of magnitude `M` on a given `Nep245`/`Imt` `TokenId` within a settlement, the total fee credited to `fee_collector` should equal `protocol_fee.fee_ceil(M)`. Instead, when that exposure is expressed as `M` separate `TokenDiff` intents with `delta == -1` each, `fee_collected == 0`.

Code path:
- `TokenDiff::execute_intent` (`contracts/defuse/core/src/intents/token_diff.rs:41-104`) iterates over `self.diff` (a single intent's `TokenDeltas`) and for negative deltas computes:
```rust
let amount = delta.unsigned_abs();
let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
```
- `token_fee` (lines 206-216) explicitly zeroes the fee for `Nep245`/`Imt` (and `Nep171`) when `amount <= 1`:
```rust
TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
```
- Multiple `TokenDiff` intents can be included in a single `DefuseIntents.intents: Vec<Intent>` signed once by the account and executed sequentially via `Intent::execute_intent` (`contracts/defuse/core/src/intents/mod.rs:115-146`) inside one `execute_signed_intent` call (`contracts/defuse/core/src/engine/mod.rs:42-83`).
- Cross-intent/cross-signer balancing is handled independently by `TransferMatcher`/`Deltas::finalize` (`contracts/defuse/core/src/engine/state/deltas.rs`), which only checks that the *sum* of all deltas across the whole batch nets to zero per token — it has no awareness of, or interaction with, the per-intent fee computation. As long as some other intent(s) in the batch (from the same or another signer) supply matching positive deltas, the batch settles regardless of how granular the negative-delta side was split.

Exploit: the attacker (any signer, holding balance ≥ M of an `Nep245`/`Imt` `TokenId`) constructs and signs one `MultiPayload` whose `DefuseIntents.intents` contains M separate `TokenDiff` intents, each with `diff = {token_id: -1, other_token: +k}` (splitting the intended `+M`-side proceeds proportionally across the M legs, or concentrated in one of them), instead of one `TokenDiff` with `diff = {token_id: -M, other_token: +K}`. Each of the M intents independently computes `amount = 1`, hits the `Nep245`/`Imt` `amount <= 1` branch, and returns `Pips::ZERO`, so `fees_collected` stays empty for every one of the M intents, and `internal_add_balance(fee_collector, ...)` is never invoked for that token. The counterparty side (positive deltas) never pays fees regardless (fees are only taken on the negative/"token_in" side), so no compensating fee is collected elsewhere.

No existing guard prevents this: `MultiPayload::verify`, nonce/salt checks, and `TransferMatcher::finalize` only validate signature/nonce validity and batch-wide netting — none of them re-aggregate per-token deltas across intents for fee purposes before calling `token_fee`.

## Impact Explanation
The `fee_collector` account is under-credited for `Nep245`/`Imt` trades whose magnitude exceeds 1, whenever the signer (or a cooperating counterparty) chooses to express the trade as many unit-magnitude `TokenDiff` intents rather than one multi-unit intent. This directly matches the "protocol fees bypassed" Critical category: value that should have flowed to `fee_collector` never leaves the trading signer's balance. It is fully repeatable per token, per account, and per batch — any signer holding `Nep245`/`Imt` balances can apply this to every trade they make on such tokens, with no cap other than the balance they hold and the willingness of a counterparty to supply the matching positive-side legs (which itself is trivial: the counterparty can supply one aggregated intent, unaffected by the split).

## Likelihood Explanation
Preconditions are minimal and fully within the attacker's control: an existing signer account with any positive `Nep245`/`Imt` balance, a nonzero `protocol_fee` configured via `engine.state.fee()`, and a counterparty (which could even be another account controlled by the same attacker, or a normal willing trade partner) supplying the offsetting positive deltas. No privileged role, relayer key, or additional signature is required — the attacker only needs to construct their own `MultiPayload` with multiple `TokenDiff` intents instead of one, which is ordinary, unprivileged use of `execute_intents`/`simulate_intents`. Cost is negligible (just extra intents in the same payload/gas), and the technique is trivially repeatable across tokens and batches.

## Recommendation
Compute the `Nep245`/`Imt` fee-exemption threshold (`amount <= 1`) against the aggregated net negative delta for a given `TokenId` across the *entire* batch (or at least across the entire signer's payload), not per individual `TokenDiff` intent. Alternatively, remove/redesign the per-intent fee exemption for `Nep245`/`Imt` so that it cannot be defeated by intent-level decomposition, e.g., by summing all deltas per `(signer, token_id)` before invoking `token_fee`, or by tracking cumulative negative exposure per token across intents within `Engine`/`Deltas` prior to fee calculation.

## Proof of Concept
Using the `defuse-sandbox` test harness (pattern from `tests/src/tests/defuse/intents/token_diff.rs`):
1. Set up `Env::builder().fee(Pips::ONE_PERCENT)` (nonzero protocol fee), create a user with an `Nep245`/`Imt`-typed MT balance of `M = 10` units of a given `token_id`, and a counterparty with sufficient balance of another token to complete the swap.
2. **Baseline**: sign one `MultiPayload` with a single `TokenDiff` intent `{token_id: -10, other_token: +K}` from the user, paired with the counterparty's offsetting `TokenDiff`. Execute via `execute_intents`. Assert `fee_collector`'s balance of `token_id` increases by `Pips::ONE_PERCENT.fee_ceil(10)` (> 0).
3. **Exploit**: sign one `MultiPayload` whose `DefuseIntents.intents` contains 10 separate `TokenDiff` intents from the same user, each `{token_id: -1, other_token: +K/10}`, paired with matching counterparty intents summing to the same aggregate. Execute via `execute_intents`. Assert `fee_collector`'s balance of `token_id` increases by `0`.
4. Compare: baseline fee credited (`Pips::ONE_PERCENT.fee_ceil(10)`) vs. exploit fee credited (`0`) for an economically identical net trade — demonstrating the bypass.