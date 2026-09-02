The README explicitly documents this behavior as intended design, not a bug.

### Title
No vulnerability — extension self-escalation is documented intended design - (contracts/wallet/src/contract.rs)

### Summary
The wallet contract's own documentation (`contracts/wallet/README.md`) explicitly states that installed extensions "have full control over the wallet-contract instance" and can be "added or removed by the signer or **other installed extensions**." This is a designed trust model, not an unintended privilege-escalation bug.

### Finding Description
The claimed broken binding — "every `NearAction` executed by the wallet traces back to exactly one owner-signed `Request`" — is not the actual security invariant of this contract. The documented invariant is instead: "any enabled extension has the same full power over the wallet-contract instance as the original signer," per `contracts/wallet/README.md` lines 62-70. `WalletImpl::execute_extension` at [1](#0-0)  only requires a non-zero deposit and `check_extension_enabled` membership check at [2](#0-1) , then dispatches the full `Request` (including `WalletOp::AddExtension`) via `execute_request`/`execute_op` at [3](#0-2) , with no restriction that an extension cannot add another extension. This matches the documented "extension chain" pattern demonstrated in the shipped test suite, where `extension.as_extension_of(master)` funnels signed requests through `master_id::w_execute_extension()` and where an extension is added and subsequently exercises full authority, including on behalf of root, at [4](#0-3)  and in `w_init` at [5](#0-4) . Extensions are explicitly compared in the docs to access keys/delegated authority mechanisms enabling "2FA, social recovery, spending limits, session keys, etc." — all of which inherently require an extension to act with full wallet authority, including managing the extension set, without a fresh owner signature per action.

### Impact Explanation
None. The owner's single act of signing `WalletOp::AddExtension{account_id: attacker.near}` (or accepting a chain where a prior extension is compromised) is, per the documented trust model, understood to grant that extension unrestricted control equivalent to the signer, including adding/removing further extensions. This is a pre-existing, explicitly-stated design decision (with an explicit "DO NOT store significant funds" warning at [6](#0-5) ) rather than a divergence between authorized and executed actions.

### Likelihood Explanation
N/A — behavior matches documented design intent.

### Recommendation
No code change required for this specific behavior; if a narrower trust model is desired (e.g., extensions unable to add further extensions, or requiring a capability/permission bitmask per extension), that would be a design change to `WalletOp`/`State`, not a bug fix, and is out of scope for this audit question as posed.

### Proof of Concept
Not applicable — behavior is exercised and validated by the existing `crates/wallet/sdk/tests/test.rs` suite (`rotate`, `w_init` tests), which confirm that an added extension gains full wallet authority by design.

### Citations

**File:** contracts/wallet/src/contract.rs (L208-221)
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

**File:** crates/wallet/sdk/tests/test.rs (L96-107)
```rust
    let extension = extension.as_extension_of(master);

    extension
        .sign_and_send(Request::new())
        .await
        .unwrap()
        .status(&near)
        .wait_until::<Final>()
        .await
        .unwrap()
        .result()
        .expect("extension should be able to execute requests on behalf of root");
```

**File:** crates/wallet/sdk/tests/test.rs (L150-174)
```rust
    near.contract::<WalletContract>(near.account_id())
        .w_execute_extension(
            Request::new()
                .internal([WalletOp::AddExtension {
                    account_id: extension.account_id().clone(),
                }])
                .into(),
        )
        .deposit(NearToken::from_yoctonear(1))
        .await
        .unwrap()
        .result()
        .unwrap();

    extension
        .as_extension_of(near.account_id())
        .sign_and_send(NearPromise::new(receiver.account_id()).transfer(NearToken::from_near(5)))
        .await
        .unwrap()
        .status(&near)
        .wait_until::<Final>()
        .await
        .unwrap()
        .result()
        .unwrap();
```

**File:** contracts/wallet/README.md (L6-7)
```markdown
> **WARN:** This implementation has not been audited yet. **DO NOT** store any
> significant funds on it.
```
