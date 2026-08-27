### Title
Self-relay with `gas_limit == u64::MAX` in `validate_tx_relayer_data` unconditionally triggers key-deletion (`create_ban_relayer_promise`), permanently bricking eth-implicit accounts whose sole access key is a limited relayer key - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs`)

### Summary
`validate_tx_relayer_data` treats an Ethereum `gas_limit` value of exactly `U64_MAX` as `u64::MAX`, whose product with `GAS_MULTIPLIER` saturates far beyond any achievable `env::prepaid_gas()`, guaranteeing `RelayerError::InsufficientGas` on every such call. Because `rlp_execute` bans (deletes) the signer's access key on *any* `Error::Relayer(_)` whenever `env::signer_account_id() == current_account_id`, an eth-implicit account relaying its own transaction with this specific gas_limit value has its only access key destroyed, and since the Wallet Contract explicitly forbids adding a `FullAccess` key, this results in a permanently frozen account/funds.

### Finding Description
`validate_tx_relayer_data` computes:
```rust
let gas_limit = if tx.gas_limit < U64_MAX { tx.gas_limit.as_u64() } else { u64::MAX };
if env::prepaid_gas().as_gas() < gas_limit.saturating_mul(GAS_MULTIPLIER) {
    return Err(Error::Relayer(RelayerError::InsufficientGas));
}
``` [1](#0-0) 

When `tx.gas_limit` equals `U64_MAX` exactly, `gas_limit = u64::MAX`, and `u64::MAX.saturating_mul(GAS_MULTIPLIER)` saturates to `u64::MAX` (~1.8×10^19), a value orders of magnitude larger than any realistic `env::prepaid_gas()` (NEAR's protocol gas ceiling per call is on the order of 3×10^14). This makes the check unconditionally fail, always returning `Error::Relayer(RelayerError::InsufficientGas)` regardless of how much gas is actually attached.

The caller, `rlp_execute`, handles this error class as follows:
```rust
Err(Error::Relayer(_)) if env::signer_account_id() == current_account_id => {
    let promise = create_ban_relayer_promise(current_account_id);
    ...
}
``` [2](#0-1) 

`create_ban_relayer_promise` deletes the NEAR access key that signed the transaction:
```rust
fn create_ban_relayer_promise(current_account_id: AccountId) -> Promise {
    let pk = env::signer_account_pk();
    Promise::new(current_account_id).delete_key(pk).function_call_weight(...)
}
``` [3](#0-2) 

This design intentionally revokes access keys used by faulty/malicious relayers, and the code comments even acknowledge self-relay: "if a relayer is using an access key for this wallet then that key will still be revoked (in the main logic of `rlp_execute`)" [4](#0-3) . However, the ban logic makes no distinction between a third-party relayer's misbehavior and the account owner accidentally (or via a malicious wallet frontend) using a sentinel/boundary `gas_limit` value that is mathematically guaranteed to fail regardless of gas attached. Recovery is impossible because the Wallet Contract explicitly rejects re-adding a full-access key as an action:
```rust
near_action::AccessKeyPermission::FullAccess => {
    Err(Error::User(UserError::UnsupportedAction(UnsupportedAction::AddFullAccessKey)))
}
``` [5](#0-4) 

If the deleted key was the account's only access key (a realistic configuration for an eth-implicit account that only ever uses a `FunctionCall`-restricted key scoped to `rlp_execute` to self-relay, as demonstrated by the existing `register_relayer`/self-relay test patterns [6](#0-5) ), the account permanently loses all ability to call any contract method, including adding any new key, freezing any NEAR/token balance held by that account forever.

### Impact Explanation
This is a permanent freezing-of-funds bug: any account that relies solely on a limited (`FunctionCall`) access key to self-relay its own eth-style transactions can have that key irrecoverably deleted by crafting (or being tricked by a malicious wallet UI/library into signing) an RLP transaction with `gas_limit` set to exactly `U64_MAX`. Because `AddFullAccessKey` is explicitly disallowed by the contract's action set, there is no on-chain recovery path once the sole key is gone, matching the "permanent freezing of user funds" bounty category.

### Likelihood Explanation
Preconditions: the account must be self-relaying (i.e., calling `rlp_execute` using an access key belonging to the contract's own account, e.g. a `FunctionCall`-scoped key limited to `rlp_execute`), and its only access key must be this key (no separate full-access key retained). Given this setup, the attacker (who may simply be the account owner, or a malicious wallet/dApp interacting with the account's signing flow) needs only to craft a single RLP transaction with `gas_limit == 2^64 - 1` — a trivially reachable, cost-free, fully attacker/user-controlled field with no additional privilege required. The bug is deterministic and 100% reproducible on the first attempt; no race conditions or timing dependencies exist.

### Recommendation
Do not treat `gas_limit == U64_MAX` (or any value causing `saturating_mul` overflow) as an automatic, unconditionally-failing `RelayerError`. Specifically:
1. Reject grossly out-of-range `gas_limit` values (e.g., values whose `GAS_MULTIPLIER`-scaled cost exceeds the protocol's maximum possible attached gas) as a `UserError` instead of a `RelayerError`, since this is intrinsically a malformed/unsatisfiable user-signed value, not a relayer failure.
2. In `rlp_execute`, do not delete the signer's own key when `Error::Relayer(RelayerError::InsufficientGas)` arises from a value that can never be satisfied by any relayer (i.e., add a sanity bound on `tx.gas_limit` before classifying it as relayer-fault), or skip the self-ban path entirely when doing so would delete the account's last remaining access key.

### Proof of Concept
Integration test plan (extends existing `tests/relayer.rs` patterns):
1. Deploy a Wallet Contract for an eth-implicit account and add only a single `FunctionCall` access key restricted to `rlp_execute` (mirroring `register_relayer`), with no full-access key retained.
2. Craft an RLP-encoded Ethereum transaction (`Transaction2930`) targeting the wallet's own account, with `gas_limit: U256::from(u64::MAX)` and a valid nonce/target/chain id.
3. Call `rlp_execute` using the wallet's own restricted access key as the NEAR transaction signer (self-relay), attaching maximal gas.
4. Assert the response is a "faulty relayer" error (`ExecuteResponse.error == Some("Error: faulty relayer")`), matching `validate_tx_relayer_data`'s guaranteed `InsufficientGas`.
5. Query `view_access_key` for the deleted key's public key and assert it returns `UnknownAccessKey` (key deleted), analogous to `assert_revoked_key` in `tests/relayer.rs` [7](#0-6) .
6. Assert that no other access key exists on the account (`view_access_key_list` returns empty), and that any subsequent attempt to call `rlp_execute` or any other method fails with `AccessKeyNotFound`/"account has no access keys", demonstrating the account and its funds are permanently unreachable.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L361-365)
```rust
    // Relayers must attach at least as much gas as the user requested.
    let gas_limit = if tx.gas_limit < U64_MAX { tx.gas_limit.as_u64() } else { u64::MAX };
    if env::prepaid_gas().as_gas() < gas_limit.saturating_mul(GAS_MULTIPLIER) {
        return Err(Error::Relayer(RelayerError::InsufficientGas));
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L121-125)
```rust
            Err(Error::Relayer(_)) if env::signer_account_id() == current_account_id => {
                let promise = create_ban_relayer_promise(current_account_id);
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L401-404)
```rust
            // Note: if a relayer is using an access key for this wallet then that key will
            // still be revoked (in the main logic of `rlp_execute`). This fact together with
            // the condition that there only be one in-flight transaction at a time implies
            // that a relayer cannot maliciously burn a large portion of the user's tokens.
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L484-487)
```rust
        near_action::Action::AddKey(action) => match action.access_key.permission {
            near_action::AccessKeyPermission::FullAccess => {
                Err(Error::User(UserError::UnsupportedAction(UnsupportedAction::AddFullAccessKey)))
            }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L503-512)
```rust
fn create_ban_relayer_promise(current_account_id: AccountId) -> Promise {
    let pk = env::signer_account_pk();
    Promise::new(current_account_id).delete_key(pk).function_call_weight(
        "ban_relayer".into(),
        Vec::new(),
        NearToken::from_yoctonear(0),
        Gas::from_tgas(1),
        GasWeight(1),
    )
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/relayer.rs (L278-324)
```rust
// A relayer sending a transaction without sufficient gas is a ban-worthy offense.
#[tokio::test]
async fn test_relayer_insufficient_gas() -> anyhow::Result<()> {
    let TestContext { worker, mut wallet_contract, wallet_sk, wallet_address, .. } =
        TestContext::new().await?;

    let relayer_pk = wallet_contract.register_relayer(&worker).await?;

    // Relayer does not attach enough gas
    let attached_gas = NearGas::from_tgas(30);
    let requested_gas = attached_gas.as_gas() / crate::internal::GAS_MULTIPLIER + 100;
    let transaction = aurora_engine_transactions::eip_2930::Transaction2930 {
        nonce: 0.into(),
        gas_price: 0.into(),
        gas_limit: requested_gas.into(),
        to: Some(Address::new(wallet_address)),
        value: Wei::zero(),
        data: [
            crate::eth_emulation::ERC20_BALANCE_OF_SELECTOR.to_vec(),
            ethabi::encode(&[ethabi::Token::Address(wallet_address)]),
        ]
        .concat(),
        chain_id: CHAIN_ID + 1,
        access_list: Vec::new(),
    };
    let signed_transaction = crypto::sign_transaction(transaction, &wallet_sk);

    let result: ExecuteResponse = wallet_contract
        .inner
        .call(RLP_EXECUTE)
        .args_json(serde_json::json!({
            "target": wallet_contract.inner.id().as_str(),
            "tx_bytes_b64": codec::encode_b64(&codec::rlp_encode(&signed_transaction))
        }))
        .gas(attached_gas)
        .transact()
        .await?
        .into_result()?
        .json()?;

    assert!(!result.success);
    assert_eq!(result.error.as_deref(), Some("Error: faulty relayer"));

    assert_revoked_key(&wallet_contract.inner, &relayer_pk).await;

    Ok(())
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/relayer.rs (L326-334)
```rust
async fn assert_revoked_key(
    wallet_contract: &Contract,
    relayer_pk: &near_workspaces::types::PublicKey,
) {
    let key_query = wallet_contract.as_account().view_access_key(relayer_pk).await;

    let error_message = format!("{:?}", key_query.unwrap_err());
    assert!(error_message.contains("UnknownAccessKey"));
}
```
