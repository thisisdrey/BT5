### Verdict

No vulnerability found for this question.

**Reasoning:**

The claimed exploit requires that after `check_lockout` fails on the second op, the wallet's state changes from the *first* op (`signature_enabled = false`) somehow persist while the transaction as a whole aborts. That is not what happens.

`execute_request` propagates any `Err` immediately via `?`: [1](#0-0) 

`execute_op` also propagates via `?`: [2](#0-1) 

`set_signature_mode` sets `signature_enabled = false` and then calls `check_lockout`, propagating any error up: [3](#0-2) 

`remove_extension` removes the extension and calls `check_lockout`, propagating any error up: [4](#0-3) 

The chain terminates at `w_execute_signed`, which calls `.unwrap_or_else(|err| err.panic())` on any `Err`: [5](#0-4) 

In the NEAR runtime/near-sdk execution model, contract state (`self`, i.e. `WalletImpl<S>` / `State`) is only committed to storage if the exported method returns without panicking. A panic anywhere inside the method call (including one raised from deep in `execute_request` → `execute_op` → `set_signature_mode`/`remove_extension` → `check_lockout`) aborts the entire receipt and discards *all* in-memory mutations made during that call — including the earlier `signature_enabled = false` write from the first op in the batch. There is no partial commit of `internal` ops; the whole `Request.internal` vector is applied-or-reverted atomically as a side effect of NEAR's panic semantics, not because of any extra aggregate check in the code.

So for the proposed batch `[SetSignatureMode{enable:false}, RemoveExtension{last_extension}]`:
- Op 1 mutates in-memory `signature_enabled` to `false`; `check_lockout` passes because `extensions` is still non-empty.
- Op 2 removes the last extension; `check_lockout` now fails, returning `Error::Lockout`, which bubbles all the way up and causes `w_execute_signed` to panic.
- Because of the panic, the entire transaction is rolled back — the on-chain state after the call reverts to what it was *before* the call, i.e., `signature_enabled` is still `true` (or whatever it was before) and `extensions` still contains the extension.

Thus the binding `signature_enabled || !extensions.is_empty() == true` still holds after the call completes (whether it succeeds fully or reverts entirely) — the per-op `check_lockout` combined with NEAR's atomic revert-on-panic behavior is sufficient to prevent the described permanent lockout. The premise that "the SECOND op's `check_lockout` then permanently locks the wallet" is incorrect: a failing `check_lockout` prevents any state from being persisted at all, rather than allowing a partially-applied unsafe state to be committed.

### Citations

**File:** contracts/wallet/src/contract.rs (L112-116)
```rust
    #[inline]
    fn w_execute_signed(&mut self, msg: RequestMessage, proof: String) {
        self.execute_signed(msg, &proof)
            .unwrap_or_else(|err| err.panic());
    }
```

**File:** contracts/wallet/src/contract.rs (L224-234)
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
```

**File:** contracts/wallet/src/contract.rs (L236-242)
```rust
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
