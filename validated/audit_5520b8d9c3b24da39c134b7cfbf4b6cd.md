### Title
`check_lockout` only verifies `signature_enabled`/`extensions.is_empty()`, not actual signability, permanently locking wallets deployed with an always-false `SignatureSchema` - ([File: contracts/wallet/src/contract.rs])

### Summary
`check_lockout()` treats `signature_enabled == true` as sufficient to permit removing the last extension, without ever checking whether the configured `SignatureSchema` (e.g. `NoSign`) can actually produce a valid proof. This lets the last remaining enabled extension chain `SetSignatureMode{enable:true}` and `RemoveExtension{self}` in a single `Request`, passing `check_lockout()` at both steps while leaving the wallet in a state where neither `w_execute_signed` (schema always returns `false`) nor `w_execute_extension` (no extensions left) can ever succeed again.

### Finding Description
The broken binding claimed correct is: `is_signature_allowed() == true` implies "there exists a reachable path (`S::verify_request_msg` can return `true` for some `proof`)". This binding is false for schemas like `NoSign`, whose `verify_request_msg` is hardcoded to return `false` [1](#0-0) .

`check_lockout()` only inspects the `signature_enabled` flag and whether `extensions` is empty, never whether the schema `S` can ever authenticate anything: [2](#0-1) .

The exploit path: as the last enabled extension, call `w_execute_extension` with `Request{ internal: [WalletOp::SetSignatureMode{enable:true}, WalletOp::RemoveExtension{account_id: self}] }`. `execute_request` processes ops sequentially via `execute_op` [3](#0-2) :
1. `set_signature_mode(true, ..)` sets `signature_enabled = true` then calls `check_lockout()`, which passes because `signature_enabled` is now `true` [4](#0-3) .
2. `remove_extension(self, ..)` removes the last extension, then calls `check_lockout()` again; since `signature_enabled == true`, the condition `!signature_enabled && extensions.is_empty()` is `false`, so it passes [5](#0-4) .

The whole `Request` returns `Ok(())`. State ends up with `signature_enabled = true`, `extensions = {}`, and `public_key = NoPublicKey` (or any schema whose verification is always `false`). From this point:
- `w_execute_signed` checks `is_signature_allowed()` — which simply returns `signature_enabled || extensions.is_empty()`, i.e. `true` [6](#0-5)  — so it proceeds, but then `S::verify_request_msg` always returns `false`, so `Err(Error::InvalidSignature)` is returned forever [7](#0-6) .
- `w_execute_extension` requires `predecessor_account_id` to be an enabled extension via `check_extension_enabled`, but `extensions` is now empty, so it always fails [8](#0-7) , [9](#0-8) .

No other guard intervenes: `check_lockout()` is the only safeguard against this state and it is defeated by ordering the two ops within one atomic `Request`.

### Impact Explanation
Once this sequence executes, the wallet contract becomes permanently unauthorizable: no signature can ever verify (schema is `NoSign`/always-false), and no extension exists to call `w_execute_extension`. Any Verifier balances tied to this wallet's `AccountId` (as `signer_id`) become permanently frozen because no future `Request`, and therefore no `execute_request` (withdrawals, transfers, or any wallet-authorized intent), can ever be constructed again. This is a one-shot, irreversible action per wallet instance — matches the "user funds permanently frozen" Critical impact category.

### Likelihood Explanation
Preconditions: the actor must already be an enabled extension of the wallet (added via `AddExtension`, presumably as part of a legitimate "1-of-M / fan-out" configuration using the `NoSign` schema as documented in the crate) and must be the last remaining extension. Given that precondition, the attack costs a single `w_execute_extension` call with 1 yoctoNEAR deposit and no special privileges beyond being that extension — it is a one-line malicious/compromised-extension action, fully repeatable across any wallet deployed with `NoSign` (or any schema whose `verify_request_msg` can be permanently false) as its sole authentication mechanism after the last extension is removed.

### Recommendation
Change `check_lockout()` to reject any state transition that would leave the wallet with `extensions.is_empty()` while the enabled signature schema can never produce a valid signature (e.g., disallow enabling `signature_enabled` for schemas known to be non-verifiable, or require that removing the last extension is only allowed if `signature_enabled` was already `true` *before* this same request began, not merely toggled within it). More robustly, evaluate `check_lockout()` against the *pre-request* state for `signature_enabled` (snapshot at the start of `execute_request`), or disallow `SetSignatureMode{enable:true}` immediately followed by removal of the last extension within a single request when the schema is statically known to always fail verification.

### Proof of Concept
```
cargo test -p defuse-wallet --features no-sign -- lockout_via_toggle

// Setup: WalletImpl<NoSign> with state.signature_enabled = false,
// extensions = {extension_acc}, public_key = NoPublicKey.

// Step 1: call execute_extension as `extension_acc` with:
// Request { internal: [
//     WalletOp::SetSignatureMode { enable: true },
//     WalletOp::RemoveExtension { account_id: extension_acc },
// ], external: [] }
let result = wallet.execute_extension(request);
assert!(result.is_ok()); // passes check_lockout() at both steps

// Binding check (before vs after):
// Before: is_signature_allowed() == true is claimed to imply exists proof s.t. NoSign::verify_request_msg(...) == true
// After: state.signature_enabled == true, state.extensions.is_empty() == true
assert!(wallet.0.is_signature_allowed()); // true
// But:
assert!(!NoSign::verify_request_msg(&NoPublicKey, &any_msg, &any_proof)); // always false, for ALL proof

// Step 2: prove permanent lock
let err1 = wallet.execute_signed(any_msg, "any-proof");
assert_eq!(err1, Err(Error::InvalidSignature)); // can never succeed, for ANY proof

let err2 = wallet.execute_extension(any_request); // predecessor not in extensions
assert_eq!(err2, Err(Error::ExtensionNotEnabled(_)));
```

### Citations

**File:** contracts/wallet/signatures/no-sign/src/lib.rs (L21-28)
```rust
    #[inline]
    fn verify_request_msg(
        _public_key: &Self::PublicKey,
        _msg: &RequestMessage,
        _proof: &str,
    ) -> bool {
        false
    }
```

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

**File:** contracts/wallet/src/contract.rs (L208-222)
```rust
    fn execute_extension(&mut self, request: Request) -> Result<()> {
        if env::attached_deposit().is_zero() {
            return Err(Error::InsufficientDeposit);
        }

        // check whether extension is enabled
        let extension_id = env::predecessor_account_id();
        self.check_extension_enabled(&extension_id)?;

        // maybe cleanup nonces from the storage as best-effort to make it
        // available for further applying wallet-ops below
        self.0.nonces.check_cleanup();

        self.execute_request(request, &Actor::Extension(extension_id.into()))
    }
```

**File:** contracts/wallet/src/contract.rs (L224-242)
```rust
    fn execute_request(&mut self, request: Request, actor: &Actor<'_>) -> Result<()> {
        for op in request.internal {
            self.execute_op(op, actor.as_ref())?;
        }

        for promise in request.external {
            Self::build_promise(promise)?.detach();
        }

        Ok(())
    }

    fn execute_op(&mut self, op: WalletOp, actor: Actor<'_>) -> Result<()> {
        match op {
            WalletOp::SetSignatureMode { enable } => self.set_signature_mode(enable, actor),
            WalletOp::AddExtension { account_id } => self.add_extension(account_id, actor),
            WalletOp::RemoveExtension { account_id } => self.remove_extension(&account_id, actor),
        }
    }
```

**File:** contracts/wallet/src/contract.rs (L244-258)
```rust
    fn set_signature_mode(&mut self, enable: bool, actor: Actor<'_>) -> Result<()> {
        if self.0.signature_enabled == enable {
            return Err(Error::ThisSignatureModeAlreadySet);
        }
        self.0.signature_enabled = enable;
        self.check_lockout()?;

        WalletEvent::SignatureModeSet {
            enabled: enable,
            by: actor,
        }
        .emit();

        Ok(())
    }
```

**File:** contracts/wallet/src/contract.rs (L274-287)
```rust
    fn remove_extension(&mut self, account_id: &AccountIdRef, actor: Actor<'_>) -> Result<()> {
        if !self.0.extensions.remove(account_id) {
            return Err(Error::ExtensionNotEnabled(account_id.to_owned()));
        }
        self.check_lockout()?;

        WalletEvent::ExtensionRemoved {
            account_id: account_id.into(),
            by: actor,
        }
        .emit();

        Ok(())
    }
```

**File:** contracts/wallet/src/contract.rs (L289-295)
```rust
    #[inline]
    fn check_extension_enabled(&self, account_id: &AccountIdRef) -> Result<()> {
        if !self.0.has_extension(account_id) {
            return Err(Error::ExtensionNotEnabled(account_id.to_owned()));
        }
        Ok(())
    }
```

**File:** contracts/wallet/src/contract.rs (L297-303)
```rust
    #[inline]
    fn check_lockout(&self) -> Result<()> {
        if !self.0.signature_enabled && self.0.extensions.is_empty() {
            return Err(Error::Lockout);
        }
        Ok(())
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
