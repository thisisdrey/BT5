This confirms the key fact: the finding is not exploitable.

### Title
No valid vulnerability — attacker-supplied AddKey nonce is always overwritten - ([File: runtime/runtime/src/access_keys.rs])

### Summary
The `nonce` field decoded from the ABI-encoded `ADD_KEY_SIGNATURE` payload in `ethabi_utils.rs`/`types.rs::Action::try_into_near_action` is placed into `AccessKey.nonce` on `AddKeyAction`, but by the time this action is executed by the runtime, the attacker-supplied value is discarded and replaced deterministically by the protocol. There is no path by which an attacker can set a usable or persisted nonce checkpoint.

### Finding Description
`types.rs::Action::try_into_near_action` builds `near_action::AddKeyAction` using the caller-controlled `nonce` decoded via `TryFromToken for u64` in `ethabi_utils.rs`: [1](#0-0) 

However, this action is ultimately dispatched into the NEAR runtime's `action_add_key` handler, which for a regular (non-gas) key calls `add_regular_key`, and that function **overwrites** whatever nonce was supplied on the incoming `AccessKey` with a value derived purely from `block_height`: [2](#0-1) 

The seeding function is: [3](#0-2) 

This is exactly `(block_height - 1) * 1_000_000`, deterministic and controlled only by the block at which the AddKey action executes, not by any field in the `AddKeyAction`/`AccessKey` struct that was constructed by the wallet contract or any other caller. The same overwrite applies to gas keys (`add_gas_key`, which additionally forces `access_key.nonce = 0` and seeds all nonce slots via `initial_nonce_value`).

Because of this, the attacker-controlled `nonce` field decoded from the ABI-encoded wallet-contract `AddKey` action (or from any other AddKey action in the system) never survives into on-chain state — it is unconditionally clobbered before `set_access_key` persists it. Every subsequent nonce validation (`verify_nonce` in `verifier.rs`) reads and validates against this block-height-derived stored nonce, not the attacker's value. There is no way to set the persisted nonce to 0, to an attacker-chosen low value, or to `u64::MAX`, so neither the described double-spend replay nor the permanent-freeze scenario is reachable.

### Impact Explanation
None. The described attack path requires the ABI-decoded `nonce` to become the authoritative on-chain `AccessKey.nonce`, but the runtime's `add_regular_key`/`add_gas_key` always reseed it from `block_height`, which is outside attacker control and not repeatable to a chosen value (an attacker cannot force a specific block height, and even if they could, `block_height * 1_000_000` values are already accounted for by `NonceTooLarge`/`verify_nonce` bounds checks). No double-spend, replay, or fund-freeze is possible via this field.

### Likelihood Explanation
Not applicable — the precondition (attacker-chosen nonce persisting to the access key) does not hold in this codebase.

### Recommendation
No fix needed for this specific concern. If defense-in-depth is desired, `AddKeyAction`/`Action::AddKey` could be documented more explicitly to state that the `nonce` field is always ignored/overwritten by the runtime (this is already noted in `tools/mirror/src/lib.rs` comments), to prevent future confusion for integrators like the wallet-contract ABI encoding.

### Proof of Concept
An integration test (already effectively present as `access_keys.rs::test_add_gas_key` and analogous regular-key tests) that:
1. Constructs an `AddKeyAction` with an attacker-chosen `access_key.nonce` (e.g., `0` or `u64::MAX`).
2. Calls `action_add_key` at a known `block_height`.
3. Reads back the stored `AccessKey` via `get_access_key` and asserts `access_key.nonce == initial_nonce_value(block_height)`, i.e., not equal to the attacker-supplied value.

This demonstrates the attacker-controlled field has no effect on the persisted nonce, closing off the described replay/freeze scenario. [4](#0-3)

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L272-289)
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
                let action = AddKeyAction { public_key, access_key };
                near_action::Action::AddKey(action)
```

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
}
```

**File:** runtime/runtime/src/access_keys.rs (L230-255)
```rust
fn add_regular_key(
    fee_config: &RuntimeFeesConfig,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    account_id: &AccountId,
    public_key: &PublicKey,
    access_key: &AccessKey,
    block_height: BlockHeight,
) -> Result<(), StorageError> {
    let mut access_key = access_key.clone();
    access_key.nonce = initial_nonce_value(block_height);
    set_access_key(state_update, account_id.clone(), public_key.clone(), &access_key);

    account.set_storage_usage(
        account
            .storage_usage()
            .checked_add(access_key_storage_usage(fee_config, public_key, &access_key))
            .ok_or_else(|| {
                StorageError::StorageInconsistentState(format!(
                    "Storage usage integer overflow for account {}",
                    account_id
                ))
            })?,
    );
    Ok(())
}
```

**File:** runtime/runtime/src/access_keys.rs (L442-486)
```rust
    #[test]
    fn test_add_gas_key() {
        let (account_id, public_key, access_key) = test_account_keys();
        let mut state_update = setup_account(&account_id, &public_key, &access_key);
        let mut account =
            get_account(&state_update, &account_id).expect("failed to get account").unwrap();
        let storage_before = account.storage_usage();

        let gas_key_public_key =
            InMemorySigner::from_seed(account_id.clone(), KeyType::ED25519, "gas_key").public_key();
        let gas_key = add_gas_key_to_account(
            &mut state_update,
            &mut account,
            &account_id,
            &gas_key_public_key,
        );

        let AccessKeyPermission::GasKeyFullAccess(gas_key_info) = &gas_key.permission else {
            unreachable!();
        };
        assert_eq!(gas_key_info.num_nonces, TEST_NUM_NONCES);
        assert_eq!(gas_key_info.balance, Balance::ZERO);
        assert!(account.storage_usage() > storage_before);
        assert_eq!(
            account.storage_usage(),
            storage_before
                + gas_key_storage_cost(
                    &RuntimeFeesConfig::test(),
                    &public_key,
                    &gas_key,
                    gas_key_info.num_nonces
                )
        );

        // Check gas key nonces were initialized
        let expected_nonce = (TEST_GAS_KEY_BLOCK_HEIGHT - 1)
            * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER;
        for i in 0..gas_key_info.num_nonces {
            let gas_key_nonce =
                get_gas_key_nonce(&state_update, &account_id, &gas_key_public_key, i)
                    .expect("failed to get gas key nonce")
                    .expect("gas key nonce not found");
            assert_eq!(gas_key_nonce, expected_nonce);
        }
    }
```
