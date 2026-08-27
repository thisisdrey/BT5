`Allowance` (from the `near-sdk` crate) is not vendored in this repo, so I could not directly confirm from source whether `Allowance::limited(NearToken::from_yoctonear(0))` returns `None`. Based on documented `near-sdk` semantics (an allowance of `0` yoctoNEAR is treated the same as "no meaningful limit" and `Allowance::limited` rejects zero, returning `None`), the pattern in the code is a real logic hazard, but I cannot verify the exact `near-sdk` version's behavior from the indexed files, so I present this with that caveat.

### Title
Silent downgrade of a user-specified limited access-key allowance to Unlimited in the Wallet Contract's AddKey emulation - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The report describes `CoverRouter.addCoverAndCreatePools` silently granting an unlimited ERC-20 approval instead of a bounded one, because failure/edge cases are not validated and the code falls back to "unlimited" rather than failing safely. The nearcore analog is in the ETH-implicit account **Wallet Contract** (`near-wallet-contract`), which lets an Ethereum-style signer authorize NEAR `AddKey` actions (adding a `FunctionCall` access key with an allowance) through `rlp_execute`. When translating the user's requested allowance into a NEAR `Allowance`, the code silently substitutes `Allowance::Unlimited` whenever `Allowance::limited(...)` cannot represent the requested value, instead of surfacing a user error.

### Finding Description
The relevant conversion is in `action_to_promise`: [1](#0-0) 

```rust
near_action::AccessKeyPermission::FunctionCall(access) => Ok(Promise::new(target)
    .add_access_key_allowance_with_nonce(
        action.public_key,
        access.allowance.and_then(Allowance::limited).unwrap_or(Allowance::Unlimited),
        access.receiver_id,
        access.method_names.join(","),
        action.access_key.nonce,
    )),
```

`access.allowance` is itself derived earlier in `types.rs` from the RLP-encoded Ethereum transaction fields `is_limited_allowance` / `allowance`: [2](#0-1) 

If the signer sets `is_limited_allowance = true` with an `allowance` value that `Allowance::limited` treats as invalid/unrepresentable (e.g. `0` yoctoNEAR, which many `near-sdk` versions reserve to mean "no limit"/rejects as a non-positive limited value), `Allowance::limited` returns `None`. The `.and_then(...).unwrap_or(Allowance::Unlimited)` chain then **silently converts the user's intended restrictive allowance into an unlimited allowance** for the newly added `FunctionCall` access key, rather than rejecting the transaction with a user error (as is done elsewhere in this same file for other malformed inputs, e.g. `UnsupportedAction::AddFullAccessKey` at line 486).

This mirrors the reported bug class precisely: an edge case in constructing a permission grant is not explicitly validated, and the code's fallback path silently chooses the least-restrictive ("unlimited") option instead of failing safely — exactly like the CoverRouter code approving `uint256(-1)` instead of validating and bounding the approval.

### Impact Explanation
The `FunctionCallPermission.allowance` field is documented as "a balance limit to use by this access key to pay for function call gas and transaction fees" and is meant to cap how much of the account's NEAR balance a given key/relayer can spend: [3](#0-2) 

If a user (or a wallet UI/tool built on top of the Wallet Contract's ABI) intends to add a tightly bounded relayer key — for example so a semi-trusted relayer can only spend a small, capped amount of the account's NEAR on gas/fees for a specific `receiver_id`/method — but the requested bound happens to fall into the class of values `Allowance::limited` cannot represent, the resulting on-chain access key silently becomes `Unlimited` instead. This is an authorization escalation: the relayer's key ends up able to drain far more of the account's NEAR balance (limited only by `receiver_id`/`method_names` restrictions, not by any balance cap) than the account owner authorized, while the owner has no on-chain indication that the intended cap was dropped (the resulting `ExecuteResponse` reports success, not an error).

### Likelihood Explanation
This path is reachable by any ordinary user of an ETH-implicit account signing a standard `rlp_execute` "AddKey" transaction — no privileged or malicious-node access is required, only an RLP-encoded Ethereum transaction requesting a limited-allowance `FunctionCall` key with an edge-case allowance value (most plausibly `0`). The likelihood of a user hitting this depends on tooling/wallet UX (e.g., a wallet that lets a user type "0" meaning "as small as possible" rather than "unlimited"), and on the exact behavior of `Allowance::limited` in the pinned `near-sdk` version, which I could not directly confirm from the indexed sources in this repository (the `near-sdk` crate itself is an external dependency and not vendored/searchable here).

### Recommendation
Do not use `.unwrap_or(Allowance::Unlimited)` as a silent fallback. When `access.allowance` is `Some(_)` (i.e., the user explicitly requested a limited allowance) but `Allowance::limited(...)` fails to construct a valid limited allowance, the function should return a `UserError` (consistent with how `AddFullAccessKey` is already rejected) rather than silently escalating to `Allowance::Unlimited`. `Allowance::Unlimited` should only be chosen when `access.allowance` is `None` (i.e., the signer never requested a limit at all).

### Proof of Concept
Conceptual PoC (not fully verified against the exact `near-sdk` version behavior in this repo):
1. Deploy/own an ETH-implicit account with the Wallet Contract, as in `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/relayer.rs` and `integration-tests/src/tests/features/wallet_contract.rs`.
2. Sign an RLP Ethereum transaction encoding an `AddKey` action with `is_limited_allowance = true` and `allowance = 0` (per the ABI encoding in `abi_encode`/`Action::AddKey` in `integration-tests/src/tests/features/wallet_contract.rs` lines 421-466 and `types.rs` `try_into_near_action`).
3. Submit via `rlp_execute`; the resulting access key on-chain (queryable via `view_access_key`) will have `AccessKeyPermission::FunctionCall { allowance: None, .. }` (i.e., unlimited) instead of the intended zero/near-zero bound, because `action_to_promise` in `lib.rs:484-496` falls back to `Allowance::Unlimited` when `Allowance::limited(0)` returns `None`.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L484-496)
```rust
        near_action::Action::AddKey(action) => match action.access_key.permission {
            near_action::AccessKeyPermission::FullAccess => {
                Err(Error::User(UserError::UnsupportedAction(UnsupportedAction::AddFullAccessKey)))
            }
            near_action::AccessKeyPermission::FunctionCall(access) => Ok(Promise::new(target)
                .add_access_key_allowance_with_nonce(
                    action.public_key,
                    access.allowance.and_then(Allowance::limited).unwrap_or(Allowance::Unlimited),
                    access.receiver_id,
                    access.method_names.join(","),
                    action.access_key.nonce,
                )),
        },
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L272-287)
```rust
                let public_key = construct_public_key(public_key_kind, &public_key)?;
                let access_key = if is_full_access {
                    AccessKey { nonce, permission: AccessKeyPermission::FullAccess }
                } else {
                    let allowance = if is_limited_allowance { Some(allowance) } else { None };
                    AccessKey {
                        nonce,
                        permission: AccessKeyPermission::FunctionCall(FunctionCallPermission {
                            allowance: allowance.map(NearToken::from_yoctonear),
                            receiver_id: receiver_id
                                .parse()
                                .map_err(|_| Error::User(UserError::InvalidAccessKeyAccountId))?,
                            method_names,
                        }),
                    }
                };
```

**File:** core/primitives-core/src/account.rs (L889-897)
```rust
pub struct FunctionCallPermission {
    /// Allowance is a balance limit to use by this access key to pay for function call gas and
    /// transaction fees. When this access key is used, both account balance and the allowance is
    /// decreased by the same value.
    /// `None` means unlimited allowance.
    /// NOTE: To change or increase the allowance, the old access key needs to be deleted and a new
    /// access key should be created.
    pub allowance: Option<Balance>,

```
