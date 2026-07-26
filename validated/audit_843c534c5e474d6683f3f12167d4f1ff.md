### Title
Push-Payment Griefing in `vesting::distribute` Blocks All Shareholders via Abortable `deposit_coins` Loop — (`File: aptos-move/framework/aptos-framework/sources/vesting.move`)

---

### Summary

`vesting::distribute` and `staking_contract::distribute_internal` use a push-payment loop that calls `aptos_account::deposit_coins` for every recipient inside a single atomic transaction. If any one recipient's deposit aborts — which any shareholder can arrange unprivileged — the entire distribution reverts, permanently blocking all other shareholders from receiving their vested APT until the blocking condition is manually resolved.

---

### Finding Description

**`vesting::distribute`** withdraws all unlocked stake from the pool and then iterates over every shareholder, pushing coins to each via `aptos_account::deposit_coins`: [1](#0-0) 

`aptos_account::deposit_coins` contains two abort paths that a shareholder can control: [2](#0-1) 

**Abort path 1 — `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS`**: If the recipient account exists but has never registered for `AptosCoin` (i.e., was created via the lower-level `account::create_account` rather than `aptos_account::create_account`), and the account has called `set_allow_direct_coin_transfers(false)`, then `can_receive_direct_coin_transfers` returns `false` and the assert fires. [3](#0-2) 

**Abort path 2 — frozen `CoinStore`**: `coin::deposit` aborts if the recipient's `CoinStore<AptosCoin>` is frozen. [4](#0-3) 

Because Move transactions are atomic, a single abort inside the `for_each_ref` loop rolls back the entire `distribute` call, including the `withdraw_stake` that already pulled coins out of the pool. The coins return to the pool, but `distribute` cannot make progress until the blocking account is fixed.

The same pattern exists in `staking_contract::distribute_internal`: [5](#0-4) 

The formal spec for `batch_transfer` explicitly acknowledges this abort condition exists for every recipient in the loop: [6](#0-5) 

---

### Impact Explanation

A single shareholder in a vesting contract (up to `MAXIMUM_SHAREHOLDERS = 30`) can permanently prevent `distribute` from succeeding: [7](#0-6) 

All other shareholders are denied their vested APT and staking rewards for as long as the blocking condition persists. The `distribute_many` entry point compounds this: one bad contract address aborts the entire multi-contract batch: [8](#0-7) 

The admin can mitigate by calling `set_beneficiary` to redirect the malicious shareholder's distribution, but this requires admin awareness and action, and is not guaranteed to be timely.

---

### Likelihood Explanation

The trigger is unprivileged and requires only two on-chain transactions by the attacker:

1. Create an account via `account::create_account` (bypassing the APT auto-registration in `aptos_account::create_account`).
2. Call `set_allow_direct_coin_transfers(false)`.

After that, any call to `vesting::distribute` on a contract where this address is a shareholder (or beneficiary) will abort. The attacker does not need to be the transaction sender — anyone can call `distribute`.

---

### Recommendation

Replace the push-payment loop with a pull-payment model: record each shareholder's claimable balance in a table during `distribute`, and let each shareholder call a separate `claim` entry function to withdraw their own share. This isolates per-recipient failures and matches the pattern already used in `locked_coins.move`: [9](#0-8) 

If push-payment must be retained, wrap each `deposit_coins` call in a try/catch equivalent (not currently available in Move) or pre-validate that every recipient can accept the coin before withdrawing from the stake pool.

---

### Proof of Concept

```
// Step 1: Attacker sets up a blocking account (no APT registration, opt-out)
// (off-chain: create account via account::create_account, not aptos_account::create_account)
aptos_account::set_allow_direct_coin_transfers(attacker_signer, false);
// attacker does NOT call coin::register<AptosCoin>

// Step 2: Attacker is a shareholder in vesting contract at <contract_address>
// (either added by admin, or attacker is the admin and adds themselves)

// Step 3: Any caller triggers distribute — it aborts for all shareholders
vesting::distribute(<contract_address>);
// → aborts with EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS
// → all other shareholders receive nothing; coins return to stake pool
// → distribute will continue to abort on every future call until admin intervenes
```

The root cause — iterating over recipients and pushing funds atomically with no per-recipient error isolation — is identical to the Solidity `send`-in-a-loop pattern described in the seed report. [10](#0-9) [11](#0-10)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L96-97)
```text
    /// Maximum number of shareholders a vesting pool can support.
    const MAXIMUM_SHAREHOLDERS: u64 = 30;
```

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

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L758-768)
```text
    /// Call `distribute` for many vesting contracts.
    public entry fun distribute_many(contract_addresses: vector<address>) acquires VestingContract {
        let len = contract_addresses.length();

        assert!(len != 0, error::invalid_argument(EVEC_EMPTY_FOR_MANY_FUNCTION));

        contract_addresses.for_each_ref(|contract_address| {
            let contract_address = *contract_address;
            distribute(contract_address);
        });
    }
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

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L187-219)
```text
    /// Set whether `account` can receive direct transfers of coins that they have not explicitly registered to receive.
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
    }
```

**File:** aptos-move/framework/aptos-framework/sources/coin.spec.move (L333-338)
```text
    spec schema DepositAbortsIf<CoinType> {
        account_addr: address;
        let coin_store = global<CoinStore<CoinType>>(account_addr);
        aborts_if !exists<CoinStore<CoinType>>(account_addr);
        aborts_if coin_store.frozen;
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

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.spec.move (L155-163)
```text
        // deposit properties
        aborts_if exists i in 0..len(recipients):
            exists<coin::CoinStore<AptosCoin>>(recipients[i]) && global<coin::CoinStore<AptosCoin>>(recipients[i]).frozen;

        // guid properties
        aborts_if exists i in 0..len(recipients):
            account::spec_exists_at(recipients[i]) && !exists<coin::CoinStore<AptosCoin>>(recipients[i]) && global<account::Account>(recipients[i]).guid_creation_num + 2 >= account::MAX_GUID_CREATION_NUM;
        aborts_if exists i in 0..len(recipients):
            account::spec_exists_at(recipients[i]) && !exists<coin::CoinStore<AptosCoin>>(recipients[i]) && global<account::Account>(recipients[i]).guid_creation_num + 2 > MAX_U64;
```

**File:** aptos-move/move-examples/defi/sources/locked_coins.move (L178-204)
```text
    /// Recipient can claim coins that are fully unlocked (unlock time has passed).
    /// To claim, `recipient` would need the sponsor's address. In the case where each sponsor always deploys this
    /// module anew, it'd just be the module's hosted account address.
    public entry fun claim<CoinType>(recipient: &signer, sponsor: address) acquires Locks {
        assert!(exists<Locks<CoinType>>(sponsor), error::not_found(ESPONSOR_ACCOUNT_NOT_INITIALIZED));
        let locks = borrow_global_mut<Locks<CoinType>>(sponsor);
        let recipient_address = signer::address_of(recipient);
        assert!(table::contains(&locks.locks, recipient_address), error::not_found(ELOCK_NOT_FOUND));

        // Delete the lock entry both to keep records clean and keep storage usage minimal.
        // This would be reverted if validations fail later (transaction atomicity).
        let Lock { coins, unlock_time_secs } = table::remove(&mut locks.locks, recipient_address);
        locks.total_locks = locks.total_locks - 1;
        let now_secs = timestamp::now_seconds();
        assert!(now_secs >= unlock_time_secs, error::invalid_state(ELOCKUP_HAS_NOT_EXPIRED));

        let amount = coin::value(&coins);
        // This would fail if the recipient account is not registered to receive CoinType.
        coin::deposit(recipient_address, coins);

        event::emit(Claim {
            sponsor,
            recipient: recipient_address,
            amount,
            claimed_time_secs: now_secs,
        });
    }
```
