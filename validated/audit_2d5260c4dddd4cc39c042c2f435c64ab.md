I found `is_signature_allowed()` in `contracts/wallet/src/state.rs`, which uses the same `||`-instead-of-`&&` bug class as the report but breaks a genuine authorisation binding, not a mere slippage check.

### Title
Wallet permanently disables signature authentication yet still reports it as "allowed" via a faulty `||` condition - (File: contracts/wallet/src/state.rs)

### Summary
`State::is_signature_allowed()` is defined as:
```rust
pub fn is_signature_allowed(&self) -> bool {
    // allow contract to work if it was mistakenly deployed with
    // `!signature_enabled` and empty extensions.
    self.signature_enabled || self.extensions.is_empty()
}
``` [1](#0-0) 

### Finding Description
The binding this function is supposed to enforce is: *signature-based authorisation is valid if and only if `signature_enabled` is true*. Instead, the implementation uses `||`, so the predicate also returns `true` whenever `self.extensions.is_empty()` — regardless of `signature_enabled`. The intended safety property ("lockout protection": the wallet's README states *"if signature is disabled, then at least one extension must remain enabled. Otherwise, the whole request fails."*, see [2](#0-1) ) is inverted by this bug: it is precisely in the empty-extensions case that `is_signature_allowed()` should be strict, yet the `||` makes it permissive there instead.

Concretely: if a signer explicitly disables signing (`signature_enabled = false`) while no extensions are installed yet (`extensions.is_empty() == true`, e.g. before any extension has been added, or after all extensions have been removed), `is_signature_allowed()` still returns `true`. This function directly gates `execute_signed()`:
```rust
fn execute_signed(&mut self, msg: RequestMessage, proof: &str) -> Result<()> {
    if !self.0.is_signature_allowed() {
        return Err(Error::SignatureDisabled);
    }
    ...
    if !S::verify_request_msg(&self.0.public_key, &msg, proof) {
        return Err(Error::InvalidSignature);
    }
    ...
}
``` [3](#0-2) 
and `resolve_auth()`'s `Signature` branch, used for NEP-641 offchain authorization:
```rust
WalletAuthorization::Signature { msg, proof } => {
    if !self.0.is_signature_allowed() {
        return Err(Error::SignatureDisabled);
    }
    ...
}
``` [4](#0-3) 

Because the gate is bypassed, the *disabled* signature schema still authorises requests as long as the underlying `SignatureSchema::verify_request_msg` / `verify_offchain_msg` succeeds — i.e., as long as the attacker can produce a proof for the (still-fixed, never-rotatable) public key. Since the wallet's public key is immutable for its lifetime (per README, "cannot be changed later"), an attacker who compromised the private key (the entire reason the owner disabled signature auth) can continue to authorise `w_execute_signed()` requests and drain the wallet's funds via `execute_request()` → arbitrary `external` promises, even after the intended "lockout" was performed.

### Impact Explanation
This breaks the authorisation binding at the core of the wallet: *a `Request` must not execute unless the signature method that authorised it is currently enabled*. It matches the report's rubric of "High — a wallet executing an unauthorised `Request`." The failure mode is realistic: a user disables signing specifically to lock out a compromised key, but as long as no extension is enabled yet, the disable is silently ineffective and the compromised key can still move funds.

### Likelihood Explanation
The precondition (`extensions.is_empty()`) is the *default* state of every freshly-initialized wallet, per `State::new()` (`extensions: BTreeSet::new()`), see [5](#0-4) . Therefore any wallet that has never added an extension, and that calls `SetSignatureMode { enable: false }` to disable a compromised key, remains fully exposed — no privileged role, relayer, or extension is required, only knowledge of the leaked private key/proof.

### Recommendation
Change the `||` to reflect the intended one-directional escape hatch semantics, or remove the fallback entirely:
```diff
 pub fn is_signature_allowed(&self) -> bool {
-    self.signature_enabled || self.extensions.is_empty()
+    self.signature_enabled
 }
```
If the "mistaken deployment" fallback is still desired, it should only apply when the wallet has *never* had signature disabled intentionally, which cannot be safely inferred from `extensions.is_empty()` alone; a dedicated flag distinguishing "deployed misconfigured" from "user explicitly disabled" should be used instead.

### Proof of Concept
1. Deploy a wallet contract instance with default state (`signature_enabled = true`, `extensions = {}`), matching `State::new()`.
2. Attacker obtains the wallet's private key/proof capability (the compromise the owner is trying to remediate).
3. Owner sends a signed `Request` with `WalletOp::SetSignatureMode { enable: false }` via `w_execute_signed`, intending to lock out the compromised key. This succeeds and sets `signature_enabled = false`, executed via `execute_op` → `set_signature_mode`.
4. Owner has not yet added any extension, so `extensions.is_empty() == true`.
5. Attacker calls `w_execute_signed(msg, proof)` with a new malicious `Request` (e.g., transferring assets out via an `external` `NearPromise`), signing it with the still-known compromised key.
6. `execute_signed()` calls `is_signature_allowed()`, which returns `true` because `extensions.is_empty()` is `true`, despite `signature_enabled == false`.
7. `S::verify_request_msg` succeeds (attacker has the key), and `execute_request()` runs the attacker's malicious `Request`, moving funds without valid authorisation.

### Citations

**File:** contracts/wallet/src/state.rs (L47-55)
```rust
    pub const fn new(public_key: PubKey) -> Self {
        Self {
            signature_enabled: true,
            subwallet_id: DEFAULT_SUBWALLET_ID,
            public_key,
            nonces: Nonces::new(DEFAULT_TIMEOUT),
            extensions: BTreeSet::new(),
        }
    }
```

**File:** contracts/wallet/src/state.rs (L109-109)
```rust
    /// Returns whether authentication by signature is allowed
```

**File:** contracts/wallet/README.md (L72-76)
```markdown
#### Lockout protection

The contract prevents disabling all authentication methods at once: if signature
is disabled, then at least one extensions must remain enabled. Otherwise, the
whole request fails.
```

**File:** contracts/wallet/src/contract.rs (L169-200)
```rust
    fn execute_signed(&mut self, msg: RequestMessage, proof: &str) -> Result<()> {
        if !self.0.is_signature_allowed() {
            return Err(Error::SignatureDisabled);
        }

        // TODO: change to the following when External Contract Calls land:
        // if !msg.pay_for_gas && env::is_external() {
        //     return Err(Error::UnauthorizedGasPayment);
        // }
        if msg.pay_for_gas {
            env::panic_str("`pay_for_gas` is not currently supported");
        }

        // check chain_id
        if msg.chain_id != env::chain_id() {
            return Err(Error::InvalidChainId);
        }

        // check signer_id
        if msg.signer_id != env::current_account_id() {
            return Err(Error::InvalidSignerId(msg.signer_id));
        }

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
