### Title
Missing expiration bound on `OffchainMessage` authorization allows indefinite replay of wallet authorizations - (File: `contracts/wallet/src/contract.rs`)

### Summary
`WalletImpl::resolve_auth()` — the handler behind `w_resolve_auth`, which extensions call to check whether the wallet's signer authorized a given `payload` (used for session keys, spending limits, 2FA, social recovery, etc.) — only rejects messages **from the future**, but never checks that the signed message has not gone stale, and consumes no nonce for it. This is inconsistent with the sibling authorization path, `execute_signed()` (behind `w_execute_signed`), which binds every signed `RequestMessage` to a strict, bounded, single-use replay window. [1](#0-0) 

### Finding Description
The wallet contract exposes two distinct ways to authorize actions with a signature:

1. `w_execute_signed(msg: RequestMessage, proof)` → `execute_signed()`. This path commits `msg.nonce` via `Nonces::commit(nonce, created_at, timeout)`, which enforces both a lower and upper bound: `now - min(self.timeout, timeout) <= created_at <= now`, and marks the nonce as used so it cannot be replayed. [2](#0-1) [3](#0-2) 

2. `w_resolve_auth(path, authorization)` → `resolve_auth()`, used by the `WalletAuthorization::Signature { msg: OffchainMessage, proof }` variant. Here the *only* staleness/replay check performed is:
```rust
// check timestamp
if Timestamp::now() < msg.timestamp {
    return Err(Error::FromTheFuture);
}
``` [4](#0-3) 

There is no upper bound (no deadline/expiry) applied to `msg.timestamp`, and no nonce is committed or consumed for the `OffchainMessage` in this path — the check only rejects timestamps that are ahead of `block_timestamp`, but a timestamp arbitrarily far in the past is always accepted. Compare this to the design intent documented for `RequestMessage`, which explicitly enforces `now() - timeout <= created_at <= now()` for freshness, and to the nonce commit logic that treats "too old" the same as "too new" (`NonceError::ExpiredOrFuture`). [5](#0-4) [6](#0-5) 

Because `resolve_auth()` returns `AuthorizationResolution::new(msg.payload)` purely from signature validity + a "not future" timestamp check, once a signer has ever produced a valid signature over a given `(chain_id, signer_id, path, payload, timestamp)` tuple, that exact signed message remains forever "authorized" and can be resubmitted to `w_resolve_auth` an unlimited number of times, at any point in the future, by any extension that requests it.

### Impact Explanation
This breaks the authorisation-binding invariant that a signed off-chain authorization should be equivalent, in freshness guarantees, to the on-chain signed-request path: `signature validity == current, single-use authorization`. Instead: `signature validity == permanent, replayable authorization`. Any extension flow that relies on `w_resolve_auth` to gate a *time-boxed or single-use* action (e.g. a session-key grant, a one-time spending-limit increase, a social-recovery approval) can have that authorization replayed indefinitely, since neither expiry nor a nonce constrains it. This is a wallet executing an unauthorized/no-longer-valid `Request` — an old, intentionally time-limited signed authorization can still be used to unlock extension-driven wallet operations long after the signer believed it expired or after intending it as one-time use, directly matching the "wallet executing an unauthorised `Request`" High-impact category.

### Likelihood Explanation
Likelihood is contingent on how first-party or third-party extensions use `w_resolve_auth`/`WalletAuthorization::Signature` (e.g. whether they independently enforce a nonce or freshness window at the extension level, and whether the exact payload/path re-submission is possible in the extension's own state machine). The contract itself provides no mitigation, so any extension that assumes the wallet's `w_resolve_auth` performs standard replay/staleness protection (as it does for `w_execute_signed`) inherits this gap.

### Recommendation
Enforce the same staleness window used for `RequestMessage` on `OffchainMessage.timestamp` in `resolve_auth()`, e.g. require `now - timeout <= msg.timestamp <= now` for some contract- or message-defined timeout, and/or require extensions to supply and the wallet to track a nonce for `OffchainMessage` so each signed authorization can be consumed at most once, mirroring `Nonces::commit()` used in `execute_signed()`.

### Proof of Concept
1. Signer signs an `OffchainMessage` with `path = ["extension.near"]`, `payload = "grant-session-key:X"`, `timestamp = T0`, intending it to authorize a single, time-limited action.
2. Extension calls `w_resolve_auth(path, WalletAuthorization::Signature{msg, proof})`; `resolve_auth()` checks only `Timestamp::now() < msg.timestamp` (false, since `now >= T0`), verifies the signature, and returns `AuthorizationResolution::new(payload)`. [7](#0-6) 
3. Arbitrarily far in the future (`now >> T0`), the same `(msg, proof)` pair is resubmitted to `w_resolve_auth`; the check `Timestamp::now() < msg.timestamp` is still false, the signature is still valid, and the payload is authorized again — with no record anywhere in `WalletImpl` state that this exact message was already used. [8](#0-7)

### Citations

**File:** contracts/wallet/src/contract.rs (L192-200)
```rust
        // commit the nonce
        self.0
            .nonces
            .commit(msg.nonce, msg.created_at, msg.timeout)?;

        // verify signature
        if !S::verify_request_msg(&self.0.public_key, &msg, proof) {
            return Err(Error::InvalidSignature);
        }
```

**File:** contracts/wallet/src/contract.rs (L358-391)
```rust
        Ok(match input {
            WalletAuthorization::Signature { msg, proof } => {
                if !self.0.is_signature_allowed() {
                    return Err(Error::SignatureDisabled);
                }

                // check chain_id
                if msg.chain_id != env::chain_id() {
                    return Err(Error::InvalidChainId);
                }

                // check signer_id
                if msg.signer_id != env::current_account_id() {
                    return Err(Error::InvalidSignerId(msg.signer_id));
                }

                // check path
                if msg.path != path {
                    return Err(Error::InvalidPath);
                }

                // check timestamp
                if Timestamp::now() < msg.timestamp {
                    return Err(Error::FromTheFuture);
                }

                // verify signature
                if !S::verify_offchain_msg(&self.0.public_key, &msg, &proof) {
                    return Err(Error::InvalidSignature);
                }

                // authorize the payload
                AuthorizationResolution::new(msg.payload)
            }
```

**File:** contracts/wallet/src/nonces.rs (L113-133)
```rust
    #[cfg(feature = "std")]
    pub fn commit(
        &mut self,
        nonce: u32,
        created_at: Timestamp,
        timeout: Duration,
    ) -> Result<(), NonceError> {
        self.check_cleanup();

        let now = Timestamp::now();
        // check that `created_at` is in `[now - min(self.timeout, msg.timeout), now]`
        if !(now - self.timeout.min(timeout) <= created_at && created_at <= now) {
            return Err(NonceError::ExpiredOrFuture);
        }

        if self.old.get_bit(nonce) || self.current.set_bit(nonce) {
            return Err(NonceError::AlreadyUsed);
        }

        Ok(())
    }
```

**File:** contracts/wallet/README.md (L96-101)
```markdown
> **NOTE**: The contract ensures that `now() - timeout <= created_at <= now()`,
> where `now()` is the current block timestamp. Due to the desentralized nature
> of consensus in blockchains, block timestamps usually lag a bit behind the
> actual time when it's produced. As a result, clients are recommended to set
> `created_at` slightly (e.g. 60 seconds) before the actual time of signing, so
> that it doesn't fail on-chain if it arrives too fast.
```
