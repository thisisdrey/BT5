### No vulnerability found for this question.

The attack described requires `has_public_key(victim.near, attacker_pk)` to return `true` for an attacker-chosen `attacker_pk` while `victim.near` is a "victim" account with no registered keys. Tracing `has_public_key`, when no `Account` entry exists, the implementation falls back to `account_id == public_key.to_implicit_account_id()` [1](#0-0) , and the same pattern is used once an `Account` exists via `Account::has_public_key` checking `me == public_key.to_implicit_account_id()` [2](#0-1) .

For this fallback to return `true`, `victim.near` must equal the deterministic implicit account id derived from the attacker's own Ed25519 public key. Since implicit account ids are a one-way hash-derived function of the public key, the attacker cannot pick an arbitrary pre-existing "victim.near" and find a colliding keypair — they can only ever compute the implicit id that their own freshly generated key already deterministically maps to. In other words, the only accounts reachable this way are accounts that are cryptographically owned by the attacker's own key by construction (self-derived implicit accounts), not third-party victim accounts, and since no `Account` entry exists there is no state or balance to steal in the first place. This matches the question's own caveat that this path is "not forceable for an arbitrary existing named account" — and further, it isn't forceable for any *arbitrary pre-existing implicit* account either, since implicit ids are one-way derived from the key, not chosen by the attacker independent of the key. There is no reachable case where `has_public_key(victim.near, attacker_pk)` returns `true` for a `victim.near` that holds funds or was set up by someone other than the holder of `attacker_pk`.

The `TonConnectPayload::extract_defuse_payload` code itself indeed only validates `deadline`/`timestamp` and returns `signer_id` from the untrusted payload body without binding it to the verified public key [3](#0-2) , but the engine's subsequent `has_public_key` check (called after `extract_defuse_payload`) correctly rejects any `signer_id`/`public_key` pair that wasn't explicitly registered via `add_public_key` or isn't the attacker's own self-derived implicit account, closing the gap described in the question for any account with actual value at risk.

### Citations

**File:** contracts/defuse/src/contract/intents/state.rs (L42-50)
```rust
    fn has_public_key(&self, account_id: &AccountIdRef, public_key: &PublicKey) -> bool {
        self.accounts
            .get(account_id)
            .map(Lock::as_inner_unchecked)
            .map_or_else(
                || account_id == public_key.to_implicit_account_id(),
                |account| account.has_public_key(account_id, public_key),
            )
    }
```

**File:** contracts/defuse/src/contract/accounts/account/mod.rs (L86-90)
```rust
    #[inline]
    pub fn has_public_key(&self, me: &AccountIdRef, public_key: &PublicKey) -> bool {
        !self.is_implicit_public_key_removed() && me == public_key.to_implicit_account_id()
            || self.public_keys.contains(public_key)
    }
```

**File:** contracts/defuse/core/src/payload/ton_connect.rs (L63-84)
```rust
    fn extract_defuse_payload(self) -> Result<DefusePayload<T>, Self::Error> {
        let TonConnectPayloadSchema::Text { text } = self.payload else {
            return Err(Error::custom("only text payload supported"));
        };

        let p: DefusePayload<T> = serde_json::from_str(&text)?;

        // TON Connect [specification](https://docs.tonconsole.com/academy/sign-data#in-a-smart-contract-on-chain)
        // requires to check that "timestamp is recent". We don't have fixed TTL
        // for off-chain signatures but rather check if `deadline` is not expired.
        //
        // At first, we were asserting `(timestamp <= now())`, but that  was causing
        // `simulate_intents()` to fail, since sometimes signed intent is simulated
        // right after signing.
        //
        // So, we ended up to assert at least following:
        if p.deadline < self.timestamp {
            return Err(Error::custom("deadline < timestamp"));
        }

        Ok(p)
    }
```
