Based on my investigation, I found a valid analog vulnerability in the fee-exemption logic of `TokenDiff` intents.

### Title
Protocol fee on `TokenDiff` bypassed by splitting NEP-245/IMT token amounts into unit-sized intents - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
The Ethos Network bonding-curve bug allowed an attacker to pay less than intended by fragmenting a single economically meaningful action into several smaller actions, because the price/fee formula was evaluated per-call on a local ratio instead of on the true cumulative amount. The same bug class exists in `TokenDiff::token_fee()`, which grants a **complete fee exemption** (`Pips::ZERO`) whenever the per-intent `amount <= 1` for `Nep245`/`Imt` tokens. Because this check is applied per individual `TokenDiff` intent rather than to the cumulative amount moved by an account, an attacker can split any large NEP-245/IMT transfer into many unit-sized (`amount == 1`) `TokenDiff` intents to move the full amount while paying zero protocol fee.

### Finding Description
`TokenDiff::token_fee()` decides whether a fee applies based solely on the `amount` of the *individual* intent being executed: [1](#0-0) 

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

This is invoked from `TokenDiff::execute_intent()`, where the fee is computed independently for each negative delta of each intent, using only that intent's `amount`: [2](#0-1) 

The `Engine` allows a `MultiPayload` batch (`execute_signed_intents`) to contain any number of independently-signed intents, each with its own nonce, and nets all deltas together at `finalize()`: [3](#0-2) 

Because the fee decision is per-intent instead of per-cumulative-amount, an attacker holding (or colluding with a second account holding) a NEP-245 or IMT token can replace one `TokenDiff` intent moving `N` units (which would owe `fee_ceil(N, protocol_fee)`) with `N` separate `TokenDiff` intents each moving exactly `1` unit (`amount == 1`), matched pairwise between the two accounts (the same "solver/user closure" pattern already exercised in the test suite, e.g. `solver_user_closure`): [4](#0-3) 

For each of these `N` unit-intents, `token_fee()` returns `Pips::ZERO`, so `fee_ceil(1) * 0 = 0`. Summed over `N` such intents in a single `execute_intents` call, the total fee collected is `0` instead of the `fee_ceil(N, protocol_fee)` that a single equivalent `TokenDiff` would owe. The equality that should hold — **fees owed (on the cumulative amount actually moved) == fees collected** — is broken: fees owed for size-`N` movement are `> 0`, but fees actually collected are `0`.

### Impact Explanation
This directly matches the Critical impact category "fees bypassed or over-collected." Any account (or colluding pair of accounts) transacting NEP-245 or IMT tokens through `TokenDiff` intents can completely evade the protocol fee, regardless of the total value moved, simply by fragmenting the trade into unit-sized legs within one signed batch. Since NEP-245 is used to represent all tokens custodied by the Defuse Verifier (including wrapped NEP-141 balances internally, per NEP-245 usage across the contract), and IMT tokens are natively minted/burned assets, this allows systematic, unbounded fee-revenue loss to the protocol.

### Likelihood Explanation
High. No privileged role, relayer key, or special configuration is required — only two ordinary signer accounts (or one account signing multiple payloads that net against a counterpart) submitting a `Vec<MultiPayload>` to `execute_intents`. The exploit is purely a client-side transformation of a single trade into `N` smaller ones; there is no gas-cost or storage barrier preventing this since it's evaluated in one execution call, and NEAR gas limits for batched intents are already exercised in existing tests (e.g., `swap_many`).

### Recommendation
Compute and enforce the fee exemption threshold on the **total** absolute amount an account/token pair moves across an entire signed batch (or globally per intent-hash execution), not on each individual `TokenDiff` intent's `amount` in isolation. Alternatively, remove the `amount <= 1` fee exemption for `Nep245`/`Imt` token types entirely, since it creates an incentive to fragment trades, and instead charge the fee unconditionally like `Nep141`, reserving the exemption only for genuinely non-fungible `Nep171` tokens where `amount` is always `1`.

### Proof of Concept
Conceptually (following the pattern already present in `solver_user_closure`):
1. Attacker controls accounts `A` and `B`, both holding balances of a NEP-245 token `T` custodied by the Verifier, with protocol fee `Pips::ONE_PERCENT`.
2. Instead of signing one `TokenDiff{ diff: {T: -1000} }` matched by counter-intent `{T: +1000, T2: -X}` (which would owe `fee_ceil(1000, 1%) = 10`), attacker signs 1000 pairs of intents, each moving exactly `1` unit of `T` between `A` and `B` (and correspondingly small amounts of `T2`), submitted together in one `execute_intents(Vec<MultiPayload>)` call.
3. For every one of the 1000 legs, `TokenDiff::token_fee` returns `Pips::ZERO` because `amount == 1` for the `Nep245` token, so `fees_collected` for `T` sums to `0` across the whole batch instead of `10`.
4. The full `1000`-unit position is transferred exactly as intended, but the protocol fee is entirely bypassed. [2](#0-1) [1](#0-0)

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L59-78)
```rust
        for (token_id, delta) in &self.diff {
            if *delta == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            // add delta to signer's account
            engine
                .state
                .internal_apply_deltas(signer_id, [(token_id.clone(), *delta)])?;

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

**File:** contracts/defuse/core/src/engine/mod.rs (L32-83)
```rust
    pub fn execute_signed_intents(
        mut self,
        signed: impl IntoIterator<Item = MultiPayload>,
    ) -> Result<Transfers> {
        for signed in signed {
            self.execute_signed_intent(signed)?;
        }
        self.finalize()
    }

    fn execute_signed_intent(&mut self, signed: MultiPayload) -> Result<()> {
        // verify signed payload and get public key
        let public_key = signed.verify().ok_or(DefuseError::InvalidSignature)?;

        // calculate intent hash
        let hash = signed.hash();

        // extract NEP-413 payload
        let DefusePayload::<DefuseIntents> {
            signer_id,
            verifying_contract,
            deadline,
            nonce,
            message: intents,
        } = signed.extract_defuse_payload()?;

        // check recipient
        if verifying_contract != *self.state.verifying_contract() {
            return Err(DefuseError::WrongVerifyingContract);
        }

        self.inspector.on_deadline(deadline);

        // make sure message is still valid
        if deadline < Timestamp::now() {
            return Err(DefuseError::DeadlineExpired);
        }

        // make sure the account has this public key
        if !self.state.has_public_key(&signer_id, &public_key) {
            return Err(DefuseError::PublicKeyNotExist(signer_id, public_key));
        }

        // commit nonce
        self.verify_intent_nonce(nonce, deadline)?;
        self.state.commit_nonce(signer_id.clone(), nonce)?;

        intents.execute_intent(&signer_id, self, hash)?;
        self.inspector.on_intent_executed(&signer_id, hash, nonce);

        Ok(())
    }
```

**File:** tests/src/tests/defuse/intents/token_diff.rs (L416-440)
```rust
    let solver_delta_in = TokenDiff::closure_delta(&token_in, USER_DELTA_IN, fee).unwrap();

    // assume solver trades 1:2
    let solver_delta_out = solver_delta_in * -2;
    dbg!(solver_delta_in, solver_delta_out);

    // solver signs his intent
    let solver_commitment = solver
        .sign_defuse_payload_default(
            &env.defuse,
            [TokenDiff {
                diff: TokenDeltas::new(
                    [
                        (token_in.clone(), solver_delta_in),
                        (token_out.clone(), solver_delta_out),
                    ]
                    .into_iter()
                    .collect(),
                ),
                memo: None,
                referral: None,
            }],
        )
        .await
        .unwrap();
```
