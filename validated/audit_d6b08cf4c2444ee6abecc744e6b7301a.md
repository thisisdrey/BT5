### Title
`WalletImpl::check_lockout` treats `signature_enabled = true` as a live auth path even for signature schemas that can never verify (e.g. `NoSign`), letting the last extension permanently brick the wallet - (File: `contracts/wallet/src/contract.rs`)

### Summary
`check_lockout()` only asserts the boolean pair `(signature_enabled, extensions.is_empty())` is not `(false, true)`; it never checks whether `S::verify_request_msg` can structurally succeed for the deployed schema. For the `NoSign` schema, `verify_request_msg` is a hard-coded `false` [1](#0-0) , so flipping `signature_enabled` to `true` and then removing the last extension in the *same* `Request.internal` passes `check_lockout` twice while leaving the wallet with **no** callable authorization path at all.

### Finding Description
The intended invariant, as stated by the question, is: after every `execute_op`, `signature_enabled == true` OR `extensions` non-empty implies "at least one authorization path is always live." `check_lockout` implements only the syntactic half of this:

```
fn check_lockout(&self) -> Result<()> {
    if !self.0.signature_enabled && self.0.extensions.is_empty() {
        return Err(Error::Lockout);
    }
    Ok(())
}
``` [2](#0-1) 

`set_signature_mode` and `remove_extension` both call `check_lockout` immediately after mutating their own field [3](#0-2) , and `execute_request` aborts the whole `Request` via `?` on the first `Err`, so a naive "disable-then-remove-last" or "remove-last-then-disable" ordering is indeed caught, matching the question's premise.

However, the actual bypass uses the *opposite* transformation - **enable** signature instead of disable it:

* Precondition (matches the question's precondition exactly): extension is the LAST enabled extension, `signature_enabled == false` — this is precisely how `w_init` configures `NoSign` wallets: `s.signature_enabled = false;` with the explicit comment "so that accidentally removing self from extensions would result into lockout error" [4](#0-3) .
* Attacker (the last enabled extension) calls `w_execute_extension(Request{internal: [WalletOp::SetSignatureMode{enable: true}, WalletOp::RemoveExtension{account_id: <self>}]})`.
* Step 1 — `set_signature_mode(true, ...)`: `signature_enabled` flips `false -> true`; `check_lockout()` short-circuits on `!signature_enabled == false`, so it passes [5](#0-4) .
* Step 2 — `remove_extension(self, ...)`: `extensions.remove(self)` succeeds, `extensions` becomes empty; `check_lockout()` again short-circuits on `!signature_enabled == false` (now `true`), so it passes [6](#0-5) .
* Final state: `signature_enabled == true`, `extensions == {}`. The literal boolean binding in `check_lockout` holds (`signature_enabled == true`), so no `Error::Lockout` is ever raised and the whole `Request` commits.
* But for the `NoSign` schema, `S::verify_request_msg` is unconditionally `false` for every `(public_key, msg, proof)` [7](#0-6) , so `execute_signed` will always return `Error::InvalidSignature` regardless of `is_signature_allowed()`'s outcome [8](#0-7) . And with `extensions` now empty, `check_extension_enabled` rejects every caller in `execute_extension` [9](#0-8) [10](#0-9) . No path into `execute_request` remains reachable — permanently.

Existing guards fail because `check_lockout` reasons only about the *flag* `signature_enabled`, not about whether the concrete `SignatureSchema::verify_request_msg` can ever return `true` for that flag value. `State::is_signature_allowed()` (`signature_enabled || extensions.is_empty()`) has the same blind spot [11](#0-10) .

### Impact Explanation
Once the wallet reaches `signature_enabled == true, extensions == {}` under a schema like `NoSign` whose `verify_request_msg` can never succeed, the wallet contract's `w_execute_signed` and `w_execute_extension` are both permanently unreachable — no request, internal op, or external promise can ever be executed again. Any Verifier balances or NEAR held by this wallet account become frozen forever, matching the Critical impact category "user funds permanently frozen." The attack requires only a single call from a currently-trusted, currently-enabled extension that happens to be the last one enabled; it is a one-shot, irreversible action per wallet instance (not repeatable on the same wallet, but repeatable across every `NoSign`-schema wallet instance where the deployer added extensions in the standard "narrow purpose" pattern described by the wallet's own documentation).

### Likelihood Explanation
Preconditions are exactly the intended, documented deployment configuration for `NoSign` wallets: `signature_enabled = false` at `w_init`, at least one enabled extension [12](#0-11) . Any extension that is ever reduced to being the sole remaining enabled extension (a common, expected end-state as other extensions are rotated out) can execute this two-op `Request` at near-zero cost (1 yoctoNEAR deposit, minimal gas), with no signature or special role required — it only needs to already be a registered, enabled extension, which is within the declared attacker capability set ("a registered extension added for a narrow purpose").

### Recommendation
Make `check_lockout` (or `set_signature_mode`) schema-aware: disallow enabling `signature_enabled` (or otherwise disallow removing the last extension while relying on `signature_enabled == true` as the fallback) whenever the deployed `SignatureSchema` is structurally incapable of verification (e.g., gate this via a `SignatureSchema::CAN_VERIFY` const or equivalent, and special-case it in `WalletNoSign`'s macro-generated contract so that `SetSignatureMode{enable: true}` and removing the last extension are mutually exclusive within one `Request`, or simply forbid `WalletOp::SetSignatureMode{enable: true}` entirely for the `NoSign` variant).

### Proof of Concept
```rust
// contracts/wallet/src/contract.rs — unit test using WalletNoSign-equivalent state
#[test]
fn last_extension_can_permanently_brick_no_sign_wallet() {
    // Setup matches w_init(): signature_enabled = false, single extension = "ext.near"
    let mut wallet = WalletImpl::<NoSign>(
        State::new(NoPublicKey).extensions(["ext.near".parse().unwrap()])
    );
    assert!(!wallet.0.signature_enabled);
    assert_eq!(wallet.0.extensions.len(), 1);

    // Attacker: the last extension submits both ops in one Request
    let req = Request::new().internal([
        WalletOp::SetSignatureMode { enable: true },
        WalletOp::RemoveExtension { account_id: "ext.near".parse().unwrap() },
    ]);

    // Simulate execute_extension as predecessor == "ext.near"
    let result = wallet.execute_request(req, &Actor::Extension("ext.near".parse().unwrap()));
    assert!(result.is_ok(), "check_lockout incorrectly allows this transition");

    // Binding check: literal flag says "allowed" ...
    assert!(wallet.0.signature_enabled);
    assert!(wallet.0.extensions.is_empty());

    // ... but NO real auth path exists: verify_request_msg is always false for NoSign
    assert!(!NoSign::verify_request_msg(&NoPublicKey, &any_msg(), "any-proof"));
    // and w_execute_extension is now unreachable by anyone (extensions empty)
    assert!(wallet.check_extension_enabled(&"ext.near".parse().unwrap()).is_err());

    // => wallet permanently unauthorizable, contradicting the intended
    //    "at least one authorization path is always live" invariant.
}
```
Run with `cargo test -p defuse-wallet last_extension_can_permanently_brick_no_sign_wallet`. Also add the near-workspaces sandbox variant analogous to `crates/wallet/sdk/tests/test.rs::w_init`, replacing the final "extension executes a transfer" step with the `SetSignatureMode{enable:true} + RemoveExtension{self}` request, and assert both that the call succeeds and that any subsequent `w_execute_signed`/`w_execute_extension` call from any account fails permanently.

### Citations

**File:** contracts/wallet/signatures/no-sign/src/lib.rs (L18-28)
```rust
impl SignatureSchema for NoSign {
    type PublicKey = NoPublicKey;

    #[inline]
    fn verify_request_msg(
        _public_key: &Self::PublicKey,
        _msg: &RequestMessage,
        _proof: &str,
    ) -> bool {
        false
    }
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

**File:** contracts/wallet/src/contract.rs (L244-287)
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

    fn add_extension(&mut self, account_id: AccountId, actor: Actor<'_>) -> Result<()> {
        if !self.0.extensions.insert(account_id.clone()) {
            return Err(Error::ExtensionEnabled(account_id));
        }

        WalletEvent::ExtensionAdded {
            account_id: account_id.into(),
            by: actor,
        }
        .emit();

        Ok(())
    }

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

**File:** contracts/wallet/signatures/no-sign/src/contract.rs (L17-49)
```rust
#[near]
impl Contract {
    /// Initialize a wallet contract on the existing account
    /// with authentication by signature disabled and
    /// add the current account as an extension.
    ///
    /// This method is allowed to be called only by the current
    /// account itself. It's recommended to call this method
    /// in the same receipt right after `UseGlobalContract` action.
    ///
    /// MUST attach at least 1yN for security reasons.
    #[cfg_attr(not(near), allow(dead_code))]
    #[allow(clippy::use_self)]
    #[private]
    #[payable]
    #[init]
    pub fn w_init() -> Self {
        if env::attached_deposit().is_zero() {
            // reject FunctionCall access keys
            Error::InsufficientDeposit.panic();
        }

        let mut s = State::new(NoPublicKey)
            // Add self as the only extension
            .extensions([env::current_account_id()]);

        // Disable signature verification completely,
        // so that accidently removing self from extensions
        // would result into lockout error.
        s.signature_enabled = false;

        Self(s.into())
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
