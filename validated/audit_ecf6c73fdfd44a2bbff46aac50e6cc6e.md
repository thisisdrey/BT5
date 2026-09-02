### Title
Protocol fees bypassed by splitting a bulk NEP-245/IMT transfer into N unit-amount `TokenDiff` intents in one signed payload - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::execute_intent` computes the fee independently for each `TokenDiff` intent based only on that intent's own `delta`, and `TokenDiff::token_fee` returns `Pips::ZERO` whenever the token is `Nep171`, or `Nep245`/`Imt` with `amount <= 1`. Because a single signed `DefuseIntents` message can contain an arbitrary `Vec<Intent>` that is executed intent-by-intent, an attacker can replace one `TokenDiff{delta: -N}` with N separate `TokenDiff{delta: -1}` intents on the same `Nep245` token, reducing the total protocol fee from `Pips::fee_ceil(N)` to `0`.

### Finding Description
The broken binding: `sum(fees_collected added to fee_collector across the batch) == Pips::fee_ceil(total negative amount moved of token T)`.

`execute_intent` for `TokenDiff` loops over `self.diff` (a single intent's deltas) and computes, per negative delta:
```
let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
``` [1](#0-0) 

`token_fee` is defined as:
```
TokenIdType::Nep141 => {}
TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
``` [2](#0-1) 

Crucially, this fee exemption is evaluated per `TokenDiff` intent's own `amount`, not on the aggregate amount moved by the signer for that token across the whole message. `DefuseIntents` simply iterates `self.intents` and calls `execute_intent` on each one independently:
```
for intent in self.intents {
    intent.execute_intent(signer_id, engine, intent_hash)?;
}
``` [3](#0-2) 

`Intent::TokenDiff` is one of many variants a signer can freely stack inside a single `DefuseIntents.intents: Vec<Intent>` within one `MultiPayload`, and nonce/signature verification happens once per `MultiPayload`, not per intent, so packing N `TokenDiff` intents costs the attacker nothing extra in terms of signatures or nonces. [4](#0-3) [5](#0-4) 

Exploit: the attacker, holding `N` units of `nep245:mt.near:token1` in the Verifier, signs one `MultiPayload` whose `DefuseIntents.intents` contains N separate `TokenDiff` entries, each `diff = {"nep245:mt.near:token1": -1}` (paired with corresponding `+1` legs on another token to keep supply-delta rules satisfied, or matched by a counterparty intent, exactly as normal `TokenDiff` trading works). Each of the N `execute_intent` calls independently computes `token_fee(token_id, 1, protocol_fee) == Pips::ZERO`, so `fee_ceil(1) == 0` every time. The cumulative `fees_collected` added to `fee_collector` is `0`, whereas performing the economically-equivalent single `TokenDiff{delta: -N}` would compute `token_fee(token_id, N, protocol_fee) == protocol_fee` (nonzero, since `amount > 1`), yielding `fee_ceil(N) > 0`.

None of the existing guards address this: `MultiPayload::verify`/`has_public_key`/nonce commit only ensure the payload is authentically signed and used once — they do not constrain how many `Intent`s or how fee-relevant amounts are partitioned within that one signed payload. There is no aggregation of `TokenDiff` deltas per token across intents before fee calculation.

### Impact Explanation
Value that should have flowed to `fee_collector` (`engine.state.fee_collector()`) via `internal_add_balance` is instead retained by the attacker, i.e. protocol fees are bypassed on Nep245/Imt token transfers of any size, by decomposing them into unit legs. This directly matches the Critical category "protocol fees bypassed" — it is fully repeatable, works for any signer, any amount `N`, and any `Nep245`/`Imt` token with `protocol_fee > 0`, and requires no privileged role, only a normal Verifier balance and a signature.

### Likelihood Explanation
Preconditions are trivial and fully within an unprivileged attacker's control: hold `N` units of some `Nep245`/`Imt` token inside the Verifier, and `protocol_fee > 0`. The attacker pays no extra signature or nonce cost (single `MultiPayload`, single nonce) and only marginally more gas/storage to include N intents instead of one. This is straightforward and highly feasible, limited only by gas/message-size limits (out of scope per the rules to consider as a mitigating factor, but practically this still permits meaningful fee evasion for realistic N).

### Recommendation
Compute the Nep245/Imt fee exemption based on the aggregate amount moved per token across the whole `DefuseIntents`/batch (or disallow multiple `TokenDiff` intents touching the same `(signer, token_id)` pair within a single execution), rather than per individual `TokenDiff` intent's own delta. Alternatively, sum all negative deltas for a given token across the intents vector before applying the `amount <= 1` exemption check in `TokenDiff::token_fee`.

### Proof of Concept
`cargo test` in `contracts/defuse/core` (or a `near-workspaces` sandbox test under `tests/`):
1. Set up an `Engine` with a mocked `State`/`Inspector`, `protocol_fee = Pips::ONE_PERCENT` (or any nonzero fee), and a signer account credited with `N = 100` units of `nep245:mt.near:token1`.
2. Construct a `DefuseIntents { intents: vec![TokenDiff{diff:{token1: -1, other_token: +k}}; 100] }` (100 single-unit legs) inside one `MultiPayload`, execute via `execute_signed_intents`.
3. Assert `fee_collector`'s balance for `token1` after execution is `0`.
4. Compare against executing a single `TokenDiff{diff:{token1: -100, other_token: +100k}}`: assert `fee_collector`'s balance equals `Pips::ONE_PERCENT.fee_ceil(100) > 0`.
5. Assert the two total collected fees differ (`0 != Pips::fee_ceil(100)`), demonstrating the binding failure.

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

**File:** contracts/defuse/core/src/intents/mod.rs (L30-68)
```rust
pub struct DefuseIntents {
    /// Sequence of intents to execute in given order. Empty list is also
    /// a valid sequence, i.e. it doesn't do anything, but still invalidates
    /// the `nonce` for the signer
    /// WARNING: Promises created by different intents are executed concurrently and does not rely on the order of the intents in this structure
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub intents: Vec<Intent>,
}

#[cfg_attr(feature = "schemars-v0_8", derive(::schemars::JsonSchema))]
#[derive(Debug, Clone, Serialize, Deserialize, From)]
#[serde(tag = "intent", rename_all = "snake_case")]
pub enum Intent {
    /// See [`AddPublicKey`]
    AddPublicKey(AddPublicKey),

    /// See [`RemovePublicKey`]
    RemovePublicKey(RemovePublicKey),

    /// See [`Transfer`]
    Transfer(Transfer),

    /// See [`FtWithdraw`]
    FtWithdraw(FtWithdraw),

    /// See [`NftWithdraw`]
    NftWithdraw(NftWithdraw),

    /// See [`MtWithdraw`]
    MtWithdraw(MtWithdraw),

    /// See [`NativeWithdraw`]
    NativeWithdraw(NativeWithdraw),

    /// See [`StorageDeposit`]
    StorageDeposit(StorageDeposit),

    /// See [`TokenDiff`]
    TokenDiff(TokenDiff),
```

**File:** contracts/defuse/core/src/intents/mod.rs (L108-111)
```rust
        for intent in self.intents {
            intent.execute_intent(signer_id, engine, intent_hash)?;
        }
        Ok(())
```

**File:** contracts/defuse/core/src/engine/mod.rs (L42-83)
```rust
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
