No vulnerability found for this question.

The code correctly isolates NEAR-implicit account creation from attacker interference:

- `check_transfer_to_nonexisting_account` only permits implicit account creation when `implicit_account_creation_eligible` is true, which requires the Transfer to be the *only* action in the transaction/receipt — this prevents bundling a `CreateAccount`, `AddKey`, or any other action in the same receipt that could hijack the account before the real key owner acts. [1](#0-0) 
- `check_account_existence` explicitly rejects a `CreateAccount` action targeting an implicit, non-existent account id, citing exactly this hijack scenario in its comment. [2](#0-1) 
- When the implicit account is created via `action_implicit_account_creation_transfer`, the full-access key seeded on the new account is deterministically derived from the account id itself via `PublicKey::from_near_implicit_account(account_id)` — it is *always* exactly the public key that the account id encodes, never an attacker-supplied key. [3](#0-2) 
- Any subsequent action on that account (`AddKey`, `DeleteKey`, `FunctionCall`, `Transfer` beyond the first, etc.) requires the account to already exist and, for a *transaction* (not a receipt from another sender), requires a valid signature from an access key already present on that account — i.e., only the corresponding private-key holder can authorize further actions. `check_account_existence` enforces `AccountDoesNotExist` for these actions when the account isn't present. [4](#0-3) 

Thus, an attacker who knows a public key but not its private key can create the implicit account and deposit funds into it, but cannot redirect, add keys to, or otherwise control that balance — only the real key owner (who alone can produce a valid ed25519 signature for that public key) can spend it. This matches the documented design intent explicitly called out in `docs/DataStructures/Account.md`: "Implicit account can not be created using `CreateAccount` action to avoid being able to hijack the account without having the corresponding private key." [5](#0-4)  This is further validated by the integration test `transfer_tokens_to_implicit_account`, which asserts the created account's access key matches exactly the transferred-to public key. [6](#0-5) 

No attacker-reachable bypass exists in this path.

### Citations

**File:** runtime/runtime/src/actions.rs (L224-243)
```rust
    *actor_id = account_id.clone();
    match account_id.get_account_type() {
        AccountType::NearImplicitAccount => {
            let mut access_key = AccessKey::full_access();
            access_key.nonce = initial_nonce_value(block_height);

            // unwrap: here it's safe because the `account_id` has already been determined to be implicit by `get_account_type`
            let public_key = PublicKey::from_near_implicit_account(account_id).unwrap();

            *account = Some(Account::new(
                deposit,
                Balance::ZERO,
                AccountContract::None,
                fee_config.storage_usage_config.num_bytes_account
                    + public_key.trie_id_len() as u64
                    + borsh::object_length(&access_key).unwrap() as u64
                    + fee_config.storage_usage_config.num_extra_bytes_record,
            ));

            set_access_key(state_update, account_id.clone(), public_key, &access_key);
```

**File:** runtime/runtime/src/actions.rs (L795-817)
```rust
        Action::CreateAccount(_) => {
            if account.is_some() {
                return Err(ActionErrorKind::AccountAlreadyExists {
                    account_id: account_id.clone(),
                }
                .into());
            } else {
                if account_is_implicit(account_id, config.wasm_config.eth_implicit_accounts) {
                    // If the account doesn't exist and it's implicit, then you
                    // should only be able to create it using single transfer action.
                    // Because you should not be able to add another access key to the account in
                    // the same transaction.
                    // Otherwise you can hijack an account without having the private key for the
                    // public key. We've decided to make it an invalid transaction to have any other
                    // actions on the implicit hex accounts.
                    // The easiest way is to reject the `CreateAccount` action.
                    // See https://github.com/nearprotocol/NEPs/pull/71
                    return Err(ActionErrorKind::OnlyImplicitAccountCreationAllowed {
                        account_id: account_id.clone(),
                    }
                    .into());
                }
            }
```

**File:** runtime/runtime/src/actions.rs (L834-852)
```rust
        Action::DeployContract(_)
        | Action::FunctionCall(_)
        | Action::Stake(_)
        | Action::AddKey(_)
        | Action::DeleteKey(_)
        | Action::DeleteAccount(_)
        | Action::Delegate(_)
        | Action::DelegateV2(_)
        | Action::DeployGlobalContract(_)
        | Action::UseGlobalContract(_)
        | Action::TransferToGasKey(_)
        | Action::WithdrawFromGasKey(_) => {
            if account.is_none() {
                return Err(ActionErrorKind::AccountDoesNotExist {
                    account_id: account_id.clone(),
                }
                .into());
            }
        }
```

**File:** runtime/runtime/src/actions.rs (L857-877)
```rust
fn check_transfer_to_nonexisting_account(
    config: &RuntimeConfig,
    account_id: &AccountId,
    implicit_account_creation_eligible: bool,
) -> Result<(), ActionError> {
    if implicit_account_creation_eligible
        && account_is_implicit(account_id, config.wasm_config.eth_implicit_accounts)
    {
        // OK. It's implicit account creation.
        // Notes:
        // - Transfer action has to be the only action in the transaction to avoid
        // abuse by hijacking this account with other public keys or contracts.
        // - Refunds don't automatically create accounts, because refunds are free and
        // we don't want some type of abuse.
        // - Account deletion with beneficiary creates a refund, so it'll not create a
        // new account.
        Ok(())
    } else {
        Err(ActionErrorKind::AccountDoesNotExist { account_id: account_id.clone() }.into())
    }
}
```

**File:** docs/DataStructures/Account.md (L117-117)
```markdown
Implicit account can not be created using `CreateAccount` action to avoid being able to hijack the account without having the corresponding private key.
```

**File:** integration-tests/src/tests/standard_cases/mod.rs (L543-547)
```rust
    let view_access_key = node_user.get_access_key(&receiver_id, &public_key);
    match receiver_id.get_account_type() {
        AccountType::NearImplicitAccount => {
            assert_eq!(view_access_key.unwrap(), AccessKey::full_access().into());
        }
```
