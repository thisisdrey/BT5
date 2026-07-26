### Title
Push-Pattern Distribution in `vesting::distribute` and `staking_contract::distribute_internal` Allows Any Shareholder to Permanently Freeze All Vested APT — (`aptos-move/framework/aptos-framework/sources/vesting.move`)

---

### Summary

Both `vesting::distribute` and `staking_contract::distribute_internal` iterate over all shareholders and push APT coins to each via `aptos_account::deposit_coins`. If any recipient account exists on-chain but has no `CoinStore<AptosCoin>` and has called `set_allow_direct_coin_transfers(false)`, `deposit_coins` aborts with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS`, reverting the entire distribution. Any single shareholder can exploit this to permanently freeze all other shareholders' vested APT balances.

---

### Finding Description

**Push pattern in `vesting::distribute`:**

`vesting::distribute` iterates over every shareholder and calls `aptos_account::deposit_coins` for each one atomically: [1](#0-0) 

**Push pattern in `staking_contract::distribute_internal`:**

The same pattern appears in `staking_contract::distribute_internal`, which iterates over the distribution pool and calls `aptos_account::deposit_coins` for each recipient: [2](#0-1) 

**The abort condition in `deposit_coins`:**

`aptos_account::deposit_coins` aborts if the recipient account exists but has no `CoinStore<CoinType>` AND has `allow_arbitrary_coin_transfers = false`: [3](#0-2) 

**How a shareholder creates the blocking condition:**

`set_allow_direct_coin_transfers` is a public entry function callable by any account: [4](#0-3) 

**FA migration makes this trivially reachable:**

After the FA migration, `register_apt` only creates a primary fungible store — it does **not** create a `CoinStore<AptosCoin>`: [5](#0-4) 

So any account created via `aptos_account::create_account` post-migration has no `CoinStore<AptosCoin>`, making `coin::is_account_registered<AptosCoin>` return `false` for it. The `deposit_coins` path then hits the `can_receive_direct_coin_transfers` guard.

**`terminate_vesting_contract` is also blocked:**

`terminate_vesting_contract` calls `distribute` internally before terminating, so the same abort also prevents contract termination: [6](#0-5) 

---

### Impact Explanation

A single shareholder (or a beneficiary they control) can permanently freeze all APT held in a vesting contract. The funds are not lost from the contract's stake pool, but no distribution can ever succeed — all shareholders are denied their vested APT indefinitely. This constitutes **permanent freezing of user-controlled staking/vesting balances**, which is within the allowed impact scope.

---

### Likelihood Explanation

- The trigger is a public, unprivileged entry function (`set_allow_direct_coin_transfers`) callable by any account.
- Post-FA migration, accounts created via `aptos_account::create_account` naturally lack `CoinStore<AptosCoin>`, so no special setup is needed beyond calling `set_allow_direct_coin_transfers(false)`.
- A malicious shareholder can set this up before or after joining a vesting contract (the check happens at distribution time, not at contract creation).
- The admin has no recourse: `update_beneficiary` can redirect a shareholder's share to a different address, but the attacker can simply set the new beneficiary to another account they control with the same blocking configuration.

---

### Recommendation

Replace the push pattern with a pull pattern:

1. In `vesting::distribute` and `staking_contract::distribute_internal`, instead of calling `aptos_account::deposit_coins` directly in the loop, accumulate each shareholder's owed amount in a `SimpleMap<address, u64>` stored on-chain.
2. Add a separate `claim(shareholder: &signer, contract_address: address)` entry function that lets each shareholder pull their own funds.
3. Alternatively, wrap each `deposit_coins` call so that a failure for one recipient does not abort the entire loop — store the failed amount in a claimable mapping rather than reverting.

---

### Proof of Concept

```
1. Attacker creates an account via `aptos_account::create_account(attacker_addr)`.
   → Account exists, but only has a primary FA store (no CoinStore<AptosCoin>).

2. Attacker calls `aptos_account::set_allow_direct_coin_transfers(false)`.
   → DirectTransferConfig { allow_arbitrary_coin_transfers: false } is stored at attacker_addr.

3. Attacker is added as a shareholder (or beneficiary) in a vesting contract.

4. Anyone calls `vesting::distribute(contract_address)`.

5. The loop reaches the attacker's entry:
   aptos_account::deposit_coins(attacker_addr, share_of_coins)
   → coin::is_account_registered<AptosCoin>(attacker_addr) == false
   → can_receive_direct_coin_transfers(attacker_addr) == false
   → abort EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS

6. The entire transaction reverts. No shareholder receives any funds.
   All subsequent calls to distribute() also revert.
   terminate_vesting_contract() also reverts (it calls distribute() first).
   All vested APT is permanently frozen in the stake pool.
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

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L771-785)
```text
    public entry fun terminate_vesting_contract(admin: &signer, contract_address: address) acquires VestingContract {
        assert_active_vesting_contract(contract_address);

        // Distribute all withdrawable coins, which should have been from previous rewards withdrawal or vest.
        distribute(contract_address);

        let vesting_contract = borrow_global_mut<VestingContract>(contract_address);
        verify_admin(admin, vesting_contract);
        let (active_stake, _, pending_active_stake, _) = stake::get_stake(vesting_contract.staking.pool_address);
        assert!(pending_active_stake == 0, error::invalid_state(EPENDING_STAKE_FOUND));

        // Unlock all remaining active stake.
        vesting_contract.state = VESTING_POOL_TERMINATED;
        vesting_contract.remaining_grant = 0;
        unlock_stake(vesting_contract, active_stake);
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

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L123-130)
```text
        if (!coin::is_account_registered<CoinType>(to)) {
            assert!(
                can_receive_direct_coin_transfers(to),
                error::permission_denied(EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS)
            );
            coin::register<CoinType>(&create_signer(to));
        };
        coin::deposit<CoinType>(to, coins)
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L188-219)
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
    }
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L233-235)
```text
    public(friend) fun register_apt(account_signer: &signer) {
        ensure_primary_fungible_store_exists(signer::address_of(account_signer));
    }
```
