### No vulnerability found for this question.

**Analysis for reference:**

The check order in `execute_signed` is: `is_signature_allowed()` → `pay_for_gas` → `chain_id` → `signer_id` → nonce `commit` → `S::verify_request_msg` (signature check) → `execute_request`. [1](#0-0) 

The binding that must hold is: *a `NearAction` executes iff `S::verify_request_msg(public_key, msg, proof)` returns `true` at the moment `execute_request` is invoked* — i.e., a valid signature by the wallet's owner key existed for that exact `msg`. This check is unconditional and happens *after* all the ordering-related checks the question raises (signature-allowed, chain_id, signer_id, nonce commit). [2](#0-1) 

The only effect of checking `is_signature_allowed()` first is on *which error variant* is returned when a call fails (`Error::SignatureDisabled` vs `Error::InvalidChainId`/`InvalidSignerId`/`InvalidSignature`), which is an information/error-oracle concern, not a bypass of signature verification itself. `is_signature_allowed()` returns `signature_enabled || extensions.is_empty()`, purely a state read with no side effects. [3](#0-2) 

Even granting the attacker a perfect oracle for the exact block/transaction in which `SetSignatureMode{enable:true}` executes, and a "stolen partial `RequestMessage` missing only `proof`," the attacker still cannot produce a valid `proof` without the victim's private key — this is explicitly excluded by the rules ("attacker... hold no victim private key," and "key compromise" is out of scope). Without a valid `proof`, `S::verify_request_msg` fails and `execute_request` (and thus the `NearAction`) never runs, regardless of the timing of `SetSignatureMode`. The scenario's premise of a "compromised extension" to race the toggle is also out of the attacker's granted profile (an unprivileged attacker does not control a victim's already-enabled extension; extension compromise is a supply-chain/key-compromise scenario explicitly excluded).

Since the eventual gate — cryptographic signature verification — is unconditional and independent of check ordering, and the attacker cannot forge a valid signature, the claimed binding violation (`NearAction executed` without current, valid authorization) is not achievable through this code path.

### Citations

**File:** contracts/wallet/src/contract.rs (L169-206)
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

        let hash = msg.hash();
        WalletEvent::SignedRequest { hash }.emit();

        self.execute_request(msg.request, &Actor::SignedRequest(hash))
    }
```

**File:** contracts/wallet/src/state.rs (L109-115)
```rust
    /// Returns whether authentication by signature is allowed
    #[inline]
    pub fn is_signature_allowed(&self) -> bool {
        // allow contract to work if it was mistakenly deployed with
        // `!signature_enabled` and empty extensions.
        self.signature_enabled || self.extensions.is_empty()
    }
```
