### Title
Protocol fee bypass on `Nep245`/`Imt` token diffs via unit-delta decomposition - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee()` exempts `Nep245`/`Imt` token legs from the protocol fee whenever the per-intent delta's absolute value is `1`. Because a `TokenDiff` intent only allows one delta entry per `token_id` (it's a `BTreeMap`), an attacker can decompose what would normally be a single large-quantity trade into many separate signed `TokenDiff` intents, each with `|delta| == 1` on the same `Nep245`/`Imt` token, and submit them together in one `execute_intents` batch that still nets to zero. Every one of these unit legs is fee-exempt, so the aggregate trade pays zero protocol fee instead of `fee * amount`.

### Finding Description
`TokenDiff::execute_intent()` computes fees per token entry inside a single intent: [1](#0-0) 

The fee rate itself is computed by `token_fee()`: [2](#0-1) 

The comment states the intent is to "not take fees on NFTs and MTs with `|delta| <= 1`" — a reasonable rule for genuine NFTs (`Nep171`), where a "1-unit" transfer is the entire indivisible asset. However, the same exemption is applied to `Nep245`/`Imt`, which in this protocol are also used to represent **fungible, wrapped multi-token balances** (e.g. `nep245:<contract>:<token>` intent tokens seen throughout the test fixtures, such as `token.near:abcd` with deltas of `-200`/`200`). For these tokens, a "quantity of 1" does not represent an indivisible NFT-like unit — it's just the smallest transferable increment of a fungible balance.

Because each `TokenDiff.diff` is a `BTreeMap<TokenId, i128>` (one delta per token per intent), the fee check `amount > 1` is evaluated *per intent, per token*, not on the aggregate value moved across a batch. `execute_intents()` accepts and processes an arbitrary batch of signed `MultiPayload`s, and the invariant only requires that all deltas across the batch net to zero (per `TransferMatcher::finalize()` / `InvariantViolated::UnmatchedDeltas`) — it does not require, or even track, that any two counterparties' legs be aggregated into a single `TokenDiff` for fee-accounting purposes: [3](#0-2) 

Thus, two colluding (or self-controlled) signers can replace a single `TokenDiff{diff: {tokenA: -N, tokenB: +M}}` intent (which would incur `fee * N` on `tokenA`) with `N` separate `TokenDiff` intents each moving `-1`/`+1` on the corresponding legs, batched together in one `execute_intents()` call. Each of the `N` legs independently satisfies `amount == 1`, so `token_fee()` returns `Pips::ZERO` for every leg, and `fees_collected` for the whole batch is `0` instead of `fee * N`.

### Impact Explanation
This breaks the fees-owed-versus-fees-collected binding described in scope: the protocol is supposed to collect `fee * |delta|` on every negative delta of a fee-bearing token, but for `Nep245`/`Imt` tokens the fee can be reduced to zero regardless of the total value transferred, simply by chunking the trade into unit-sized legs within one batch. This is a systematic, repeatable fee-bypass available to any pair of unprivileged signers (or a single actor controlling both sides), and it applies to `Nep245` intent-tokens that function as fungible balances (not just true 1-of-1 NFTs), so the value that can be shielded from fees is unbounded by the token's face value — only bounded by gas/tx-size costs of submitting `N` legs.

### Likelihood Explanation
The precondition is only that the protocol fee (`Pips`) is non-zero and that the target token is typed as `Nep245`/`Imt` in `defuse-core`'s `TokenId` scheme — a normal, expected token type for wrapped/multi-token intents, not a misconfiguration. No privileged role, relayer key, or DAO action is required; the attacker only needs to sign multiple `TokenDiff` intents from account(s) they control and submit them together via the public `execute_intents(signed: Vec<MultiPayload>)` entrypoint. The only cost to the attacker is proportional additional gas/transaction size for splitting the trade into unit legs, which is economically justified whenever the fee saved exceeds that marginal cost (which is true for essentially any transfer of reasonable size once counted per-batch).

### Recommendation
Compute/enforce the fee exemption on the aggregate per-token, per-signer delta across the whole batch (or at minimum across all `TokenDiff` intents from the same signer/counterparty pair in a single `execute_intents` call) rather than per single-intent delta. Alternatively, restrict the `amount <= 1` fee exemption strictly to token types that are provably non-fungible/indivisible at the protocol level (i.e., do not apply it to `Nep245`/`Imt` at all, or require an explicit "is this an NFT-like multi-token" flag rather than inferring it from delta magnitude).

### Proof of Concept
1. Configure `Defuse` with a non-zero protocol `fee` (e.g., `Pips::ONE_PERCENT`) and `fee_collector`.
2. Attacker controls (or colludes between) `signer_a` and `signer_b`, both holding balances of an `Nep245` token `T` (e.g., `nep245:mt.near:ft1`) representing what is effectively a fungible balance of size `N` (e.g., `N = 1000`).
3. Instead of signing one `TokenDiff{diff: {T: -1000, U: +X}}` (which would charge fee on `1000`), the attacker signs `N` separate `TokenDiff` intents, each with `diff: {T: -1, U: +x}` (`x = X/N`), alternating counterpart legs from `signer_b` to keep the batch net-zero, exactly as demonstrated by the pattern in `swap_many`/`solver_user_closure` tests which submit multiple `TokenDiff` intents per account in a single `execute_intents` call: [4](#0-3) 
4. Submit all `N` legs in a single `execute_intents(signed)` call. Each leg independently hits `token_fee()`'s `amount > 1` check with `amount == 1`, returning `Pips::ZERO` per `contracts/defuse/core/src/intents/token_diff.rs:207-216`.
5. Observe `TokenDiffEvent.fees_collected` across the whole batch sums to `0`, and the `fee_collector`'s balance is unchanged, whereas a single aggregated `TokenDiff{diff: {T: -1000, ...}}` would have collected `fee * 1000`.

**Note on verification limits:** I was unable to fully confirm within this session the exact underlying semantics of `Nep245`/`Imt` `TokenIdType` variants (i.e., whether `Imt` unconditionally represents fungible "intent multi-token" balances) because the `crates/primitives/token-id/src/lib.rs` definitions were not retrieved before the tool budget was exhausted. If `Imt`/`Nep245` types are used exclusively for genuinely indivisible per-unit assets (never fungible-balance wrapping) in all deployed configurations, this reduces the practical impact of the finding; the test fixtures reviewed (e.g., `nep245:token.near:abcd` diffs of `-200`) strongly suggest fungible-style balances are represented via `Nep245`, but this should be confirmed against `crates/primitives/token-id/src/lib.rs` before treating this as fully proven.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L69-78)
```rust
            // take fees only from negative deltas (i.e. token_in)
            if *delta < 0 {
                let amount = delta.unsigned_abs();
                let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);

                // collect fee
                fees_collected
                    .add(token_id.clone(), fee)
                    .ok_or(DefuseError::BalanceOverflow)?;
            }
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L206-216)
```rust
    #[inline]
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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L265-283)
```rust
    // Finalizes all transfers, or returns unmatched deltas.
    // If unmatched deltas overflow, then Err(None) is returned.
    pub fn finalize(self) -> Result<Transfers, InvariantViolated> {
        let mut transfers = Transfers::default();
        let mut deltas = TokenDeltas::default();
        for (token_id, transfer_matcher) in self.0 {
            if let Err(unmatched) = transfer_matcher.finalize_into(&token_id, &mut transfers)
                && (unmatched == 0 || deltas.apply_delta(token_id, unmatched).is_none())
            {
                return Err(InvariantViolated::Overflow);
            }
        }
        if !deltas.is_empty() {
            return Err(InvariantViolated::UnmatchedDeltas {
                unmatched_deltas: deltas,
            });
        }
        Ok(transfers)
    }
```

**File:** tests/src/tests/defuse/intents/token_diff.rs (L223-249)
```rust
    let signed = try_join_all(accounts.iter().flat_map(move |account| {
        account.diff.iter().cloned().map(move |diff| {
            account.account.sign_defuse_payload_default(
                &env.defuse,
                [TokenDiff {
                    diff,
                    memo: None,
                    referral: None,
                }],
            )
        })
    }))
    .await
    .unwrap();

    // simulate
    env.defuse
        .simulate_intents(MultiPayloadArgs { signed: &signed })
        .await
        .unwrap()
        .into_result()
        .unwrap();

    // verify
    env.defuse_execute_intents(env.defuse.contract_id(), signed.clone())
        .await
        .unwrap();
```
