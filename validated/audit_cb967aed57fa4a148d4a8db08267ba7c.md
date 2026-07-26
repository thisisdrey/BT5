### Title
Vesting Distribution Permanently Griefed by Shareholder Opting Out of Direct Coin Transfers — (File: `aptos-move/framework/aptos-framework/sources/vesting.move`)

---

### Summary

`vesting::distribute()` uses a **push pattern** that iterates over every shareholder and calls `aptos_account::deposit_coins()` atomically. If any single recipient's deposit aborts, the entire transaction reverts. A shareholder can unprivileged-ly trigger this by (1) migrating their legacy `CoinStore<AptosCoin>` to the fungible-asset store (removing the `CoinStore` resource) and (2) calling `set_allow_direct_coin_transfers(self, false)`. After these two steps, `deposit_coins` aborts on that shareholder's address, permanently blocking distribution for every other shareholder and preventing contract termination.

---

### Finding Description

**Push loop in `vesting::distribute()`** [1](#0-0) 

The function iterates over all shareholders and calls `aptos_account::deposit_coins(recipient_address, share_of_coins)` for each one inside a single atomic transaction.

**`deposit_coins` abort path** [2](#0-1) 

Inside `deposit_coins`, if `coin::is_account_registered<CoinType>(to)` returns `false` **and** `can_receive_direct_coin_transfers(to)` returns `false`, the function aborts with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS` before any coins are deposited.

**Unprivileged trigger — step 1: remove the `CoinStore`**

Any account holder can call `migrate_to_fungible_store<AptosCoin>()`, which invokes `maybe_convert_to_fungible_store` and removes the `CoinStore<AptosCoin>` resource from their address. After this, `coin::is_account_registered<AptosCoin>(shareholder)` returns `false`. [3](#0-2) 

**Unprivileged trigger — step 2: opt out of direct transfers**

Any account holder can call `set_allow_direct_coin_transfers(self, false)`, setting `allow_arbitrary_coin_transfers = false`. [4](#0-3) [5](#0-4) 

After both steps, `deposit_coins` hits the assert at line 124–126 and aborts for that shareholder's address, reverting the entire `distribute()` call.

**Termination is also blocked**

`terminate_vesting_contract` calls `distribute(contract_address)` as its first action: [6](#0-5) 

If `distribute` reverts, `terminate_vesting_contract` also reverts, making it impossible for the admin to recover funds.

**Same pattern in `staking_contract::distribute_internal`** [7](#0-6) 

The while-loop pushes coins to each recipient atomically; a blocked recipient freezes the entire staking distribution.

---

### Impact Explanation

A single malicious (or accidentally misconfigured) shareholder can permanently freeze the vesting balances of **all other shareholders** in the contract. Because `terminate_vesting_contract` also calls `distribute` first, even the admin cannot recover the locked APT. This constitutes permanent freezing of user-controlled vesting balances — explicitly listed in the Aptos bounty allowed-impact gate.

---

### Likelihood Explanation

The two required steps (`migrate_to_fungible_store` + `set_allow_direct_coin_transfers(false)`) are both public, unprivileged entry functions callable by any account holder. A shareholder who is a participant in a vesting contract can execute them at any time before a distribution is triggered. No special role, key, or governance action is required.

---

### Recommendation

Replace the push loop with a **pull pattern**: record each shareholder's claimable amount in a per-address mapping during `distribute`, and let each shareholder call a separate `claim(contract_address)` entry function to withdraw their own share. This isolates failures to the individual claimant and prevents one bad address from blocking all others.

---

### Proof of Concept

```
// Attacker is shareholder_A in a vesting contract with shareholders [A, B, C].

// Step 1 (unprivileged): remove CoinStore so is_account_registered returns false
aptos_framework::coin::migrate_to_fungible_store<AptosCoin>(shareholder_A);

// Step 2 (unprivileged): opt out of direct coin transfers
aptos_framework::aptos_account::set_allow_direct_coin_transfers(shareholder_A, false);

// Now any call to vesting::distribute(contract_address) will:
//   1. iterate shareholders [A, B, C]
//   2. reach shareholder_A
//   3. call aptos_account::deposit_coins(A, share_of_coins)
//   4. coin::is_account_registered<AptosCoin>(A) == false  (CoinStore removed)
//   5. can_receive_direct_coin_transfers(A) == false        (opted out)
//   6. assert! fails → EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS
//   7. entire transaction reverts; B and C never receive their APT

// terminate_vesting_contract(admin, contract_address) also reverts
// because it calls distribute() first (line 775).
// All vesting balances are permanently frozen.
```

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L730-740)
```text
        // Distribute coins to all shareholders in the vesting contract.
        let grant_pool = &vesting_contract.grant_pool;
        let shareholders = &grant_pool.shareholders();
        shareholders.for_each_ref(|shareholder| {
            let shareholder = *shareholder;
            let shares = pool_u64::shares(grant_pool, shareholder);
            let amount = pool_u64::shares_to_amount_with_total_coins(grant_pool, shares, total_distribution_amount);
            let share_of_coins = coin::extract(&mut coins, amount);
            let recipient_address = get_beneficiary(vesting_contract, shareholder);
            aptos_account::deposit_coins(recipient_address, share_of_coins);
        });
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L771-775)
```text
    public entry fun terminate_vesting_contract(admin: &signer, contract_address: address) acquires VestingContract {
        assert_active_vesting_contract(contract_address);

        // Distribute all withdrawable coins, which should have been from previous rewards withdrawal or vest.
        distribute(contract_address);
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L111-131)
```text
    public fun deposit_coins<CoinType>(
        to: address, coins: Coin<CoinType>
    ) acquires DirectTransferConfig {
        if (!account::exists_at(to)) {
            create_account(to);
            spec {
                // TODO(fa_migration)
                // assert coin::spec_is_account_registered<AptosCoin>(to);
                // assume aptos_std::type_info::type_of<CoinType>() == aptos_std::type_info::type_of<AptosCoin>() ==>
                //     coin::spec_is_account_registered<CoinType>(to);
            };
        };
        if (!coin::is_account_registered<CoinType>(to)) {
            assert!(
                can_receive_direct_coin_transfers(to),
                error::permission_denied(EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS)
            );
            coin::register<CoinType>(&create_signer(to));
        };
        coin::deposit<CoinType>(to, coins)
    }
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L188-218)
```text
    public entry fun set_allow_direct_coin_transfers(
        account: &signer, allow: bool
    ) acquires DirectTransferConfig {
        let addr = signer::address_of(account);
        if (exists<DirectTransferConfig>(addr)) {
            let direct_transfer_config = borrow_global_mut<DirectTransferConfig>(addr);
            // Short-circuit to avoid emitting an event if direct transfer config is not changing.
            if (direct_transfer_config.allow_arbitrary_coin_transfers == allow) { return };

            direct_transfer_config.allow_arbitrary_coin_transfers = allow;

            emit(
                DirectCoinTransferConfigUpdated {
                    account: addr,
                    new_allow_direct_transfers: allow
                }
            );
        } else {
            let direct_transfer_config = DirectTransferConfig {
                allow_arbitrary_coin_transfers: allow,
                update_coin_transfer_events: new_event_handle<
                    DirectCoinTransferConfigUpdatedEvent>(account)
            };
            emit(
                DirectCoinTransferConfigUpdated {
                    account: addr,
                    new_allow_direct_transfers: allow
                }
            );
            move_to(account, direct_transfer_config);
        };
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L226-231)
```text
    public fun can_receive_direct_coin_transfers(
        account: address
    ): bool acquires DirectTransferConfig {
        !exists<DirectTransferConfig>(account)
            || borrow_global<DirectTransferConfig>(account).allow_arbitrary_coin_transfers
    }
```

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L724-729)
```text
    public entry fun migrate_to_fungible_store<CoinType>(
        account: &signer
    ) acquires CoinStore, CoinConversionMap, CoinInfo {
        let account_addr = signer::address_of(account);
        maybe_convert_to_fungible_store<CoinType>(account_addr);
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L888-911)
```text
        // Buy all recipients out of the distribution pool.
        while (distribution_pool.shareholders_count() > 0) {
            let recipients = distribution_pool.shareholders();
            let recipient = recipients[0];
            let current_shares = distribution_pool.shares(recipient);
            let amount_to_distribute =
                distribution_pool.redeem_shares(recipient, current_shares);
            // If the recipient is the operator, send the commission to the beneficiary instead.
            if (recipient == operator) {
                recipient = beneficiary_for_operator(operator);
            };
            aptos_account::deposit_coins(
                recipient, coin::extract(&mut coins, amount_to_distribute)
            );

            emit(
                Distribute {
                    operator,
                    pool_address,
                    recipient,
                    amount: amount_to_distribute
                }
            );
        };
```
