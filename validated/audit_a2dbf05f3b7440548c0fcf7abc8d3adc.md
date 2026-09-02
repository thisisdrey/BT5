No vulnerability found for this question.

The wallet contract's design explicitly documents that extensions have full, unscoped control equivalent to the signer's own authority, so the absence of contract-level spending-scope enforcement is a stated design property, not a violation of any binding the contract or its authors claim to uphold. [1](#0-0) [2](#0-1) [3](#0-2) 

Key points:
- The README explicitly states: "Extensions are separate third-party accounts/contracts on Near that can execute arbitrary requests on behalf of the wallet and have the same full power over the wallet-contract instance as the original signer does." [4](#0-3)  This is the documented binding — extension authority == signer authority — and the code matches it exactly: `execute_extension` only checks `check_extension_enabled` before calling `execute_request` with the exact same code path (`execute_request`) used for signed requests, with no scoping mechanism introduced. [3](#0-2) 
- Trait docs for `w_execute_extension` only require the caller to be an enabled extension with non-zero deposit attached — there is no scoping field or spend-limit check anywhere in `Request`, `WalletOp`, or `execute_op`. [2](#0-1) [5](#0-4) 
- Reaching this state requires the wallet's own signer (or an already-installed extension) to voluntarily call `AddExtension` for the attacker's extension account in the first place — an unprivileged attacker with no victim private key cannot add themselves as an extension. Thus the "attack" only fires after the legitimate owner already granted the extension full authority, exactly as documented.
- None of the stated impact categories match: Critical requires funds moved "without the owner's valid signature or authorisation" — here the owner did authorize the extension (that is the precondition for it to be callable at all), and High requires "a wallet contract executing a `Request` its owner did not authorise for this chain and account" — but the owner authorized the extension to execute arbitrary requests, which is exactly what happens.

This is a documented architectural choice (open-ended extension trust model), not a contract-enforced binding that has been broken. Any "spending-scope" enforcement is explicitly left to be optionally implemented by the extension's own logic, per the README's design philosophy, not the wallet contract itself.

### Citations

**File:** contracts/wallet/README.md (L62-70)
```markdown
### Extensions

Extensions are **separate** third-party accounts/contracts on Near that can
execute arbitrary requests on behalf of the wallet and have the same full power
over the wallet-contract instance as the original signer does. Extensions can be
added or removed by the signer or other installed extensions.

Extensions is an *open ecosystem* which enable patterns like 2FA, social
recovery, spending limits, session keys, etc.
```

**File:** contracts/wallet/src/contract.rs (L53-61)
```rust
    /// Execute a request from an [enabled extension](WalletOp::AddExtension).
    ///
    /// SHOULD be `#[payable]` and accept ANY **non-zero** attached deposit.
    ///
    /// MUST panic in following cases:
    /// * zero deposit was attached
    /// * [`env::predecessor_account_id()`](near_sdk::env::predecessor_account_id)
    ///   extension is not enabled
    fn w_execute_extension(&mut self, request: Request);
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

**File:** contracts/wallet/src/request/mod.rs (L1-44)
```rust
mod ops;

pub use self::ops::*;

pub use defuse_near_promise::*;

/// A request containing internal [operations](WalletOp) to apply and external
/// [promises](NearPromise) to create.
///
/// Used directly by [extensions](crate::contract::Wallet::w_execute_extension)
/// and wrapped in [`RequestMessage`](crate::RequestMessage) for
/// [signed requests](crate::contract::Wallet::w_execute_signed).
#[cfg_attr(feature = "arbitrary", derive(::arbitrary::Arbitrary))]
#[cfg_attr(
    feature = "serde",
    derive(::serde::Serialize, ::serde::Deserialize),
    cfg_attr(feature = "schemars-v0_8", derive(::schemars::JsonSchema))
)]
#[cfg_attr(
    feature = "borsh",
    derive(::borsh::BorshSerialize, ::borsh::BorshDeserialize),
    cfg_attr(feature = "borsh-schema", derive(::borsh::BorshSchema))
)]
#[derive(Debug, Clone, Default, PartialEq, Eq, Hash)]
pub struct Request {
    /// (optional) Ordered list of internal operations to apply.
    #[cfg_attr(
        feature = "serde",
        serde(default, skip_serializing_if = "Vec::is_empty")
    )]
    pub internal: Vec<WalletOp>,

    /// (optional) External promises to execute (fan-out).
    ///
    /// NOTE: all created promises are executed concurrently in arbitrary order
    /// and independently of each other, without waiting on previous ones to
    /// complete.
    #[cfg_attr(
        feature = "serde",
        serde(default, skip_serializing_if = "Vec::is_empty")
    )]
    pub external: Vec<NearPromise>,
}

```
