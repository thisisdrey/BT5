### Title
Single Shareholder Opt-Out Causes Permanent DoS in `vesting::distribute()` - (File: aptos-move/framework/aptos-framework/sources/vesting.move)

### Summary

`vesting::distribute()` iterates over every shareholder in a single atomic transaction and calls `aptos_account::deposit_coins` for each one. Because `deposit_coins` aborts when a recipient has opted out of direct coin transfers (`allow_arbitrary_coin_transfers = false`) and has no legacy `CoinStore<AptosCoin>` registered, a single shareholder can permanently block all other shareholders from ever receiving their vested APT distributions.

### Finding Description

`vesting::distribute()` withdraws the full unlocked stake and then distributes it to every shareholder in one transaction:

```move
shareholders.for_each_ref(|shareholder| {
    let shareholder = *shareholder;
    let shares = pool_u64::shares(grant_pool, shareholder);
    let amount = pool_u64::shares_to_amount_with_total_coins(grant_pool, shares, total_distribution_amount);
    let share_of_coins = coin::extract(&mut coins, amount);
    let recipient_address = get_beneficiary(vesting_contract, shareholder);
    aptos_account::deposit_coins(recipient_address, share_of_coins);  // ← can abort
});
``` [1](#0-0) 

`aptos_account::deposit_coins` contains the following guard:

```move
if (!coin::is_account_registered<CoinType>(to)) {
    assert!(
        can_receive_direct_coin_transfers(to),
        error::permission_denied(EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS)
    );
    coin::register<CoinType>(&create_signer(to));
};
coin::deposit<CoinType>(to, coins)
``` [2](#0-1) 

Post-FA-migration, `aptos_account::register_apt` only creates the primary fungible store — it does **not** create a legacy `CoinStore<AptosCoin>`:

```move
public(friend) fun register_apt(account_signer: &signer) {
    ensure_primary_fungible_store_exists(signer::address_of(account_signer));
}
``` [3](#0-2) 

Therefore, for any account created after the FA migration, `coin::is_account_registered<AptosCoin>` returns `false`, and the `can_receive_direct_coin_transfers` check is reached. Any account holder can call `set_allow_direct_coin_transfers(false)` to make `can_receive_direct_coin_transfers` return `false`:

```move
public entry fun set_allow_direct_coin_transfers(
    account: &signer, allow: bool
) acquires DirectTransferConfig {
``` [4](#0-3) 

Because the entire `distribute()` loop runs in one transaction with no isolation between iterations, the abort from one shareholder's `deposit_coins` call rolls back the entire transaction, including all other shareholders' distributions.

The same root cause exists in `batch_transfer_coins` (lower impact, affects only the sender's own transaction):

```move
recipients.enumerate_ref(|i, to| {
    let amount = amounts[i];
    transfer_coins<CoinType>(from, *to, amount);
});
``` [5](#0-4) 

The formal spec for `batch_transfer` explicitly acknowledges this abort condition:

```
aborts_if exists i in 0..len(recipients):
    exists<coin::CoinStore<AptosCoin>>(recipients[i]) && global<coin::CoinStore<AptosCoin>>(recipients[i]).frozen;
``` [6](#0-5) 

### Impact Explanation

All shareholders in the vesting contract are permanently blocked from receiving their APT distributions. The coins remain locked in the vesting contract's stake pool indefinitely. Additionally, `terminate_vesting_contract` internally calls `distribute()`, so the admin cannot terminate the contract either:

```move
public entry fun terminate_vesting_contract(admin: &signer, contract_address: address) acquires VestingContract {
    ...
    distribute(contract_address);  // ← will abort if any shareholder is opted out
``` [7](#0-6) 

This constitutes a permanent freeze of vesting balances for all honest shareholders, which is explicitly in scope.

### Likelihood Explanation

The trigger is fully unprivileged. Any shareholder (or the address designated as their beneficiary by the admin) can call `set_allow_direct_coin_transfers(false)` on their own account at any time. No special capability, governance vote, or privileged key is required. The attacker only needs to be a shareholder in the target vesting contract.

### Recommendation

Process each shareholder's distribution in an isolated manner. If a single recipient's deposit fails, skip that recipient and accumulate their share for a later claim, rather than aborting the entire transaction. A pattern analogous to the EVM `try-catch` fix recommended in the original report would be:

1. Replace the atomic loop with a per-shareholder claim model, or
2. Catch the abort from `deposit_coins` per iteration and store the unclaimed amount in a separate claimable balance mapping for that shareholder.

### Proof of Concept

1. Alice and Bob are shareholders in a vesting contract at `contract_address`.
2. Bob calls `aptos_account::set_allow_direct_coin_transfers(false)` on his own account. His account was created post-FA migration and has no legacy `CoinStore<AptosCoin>`.
3. Rewards accumulate and `unlock_rewards(contract_address)` is called. Stake lockup expires.
4. Anyone calls `vesting::distribute(contract_address)`.
5. The loop reaches Bob's entry: `aptos_account::deposit_coins(bob_address, bob_share)`.
6. `coin::is_account_registered<AptosCoin>(bob_address)` returns `false` (no legacy CoinStore).
7. `can_receive_direct_coin_transfers(bob_address)` returns `false` (Bob opted out).
8. `assert!` fires → `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS` → entire transaction aborts.
9. Alice receives nothing. The coins remain locked. `distribute()` will abort on every future call as long as Bob's opt-out is in place.
10. Even `terminate_vesting_contract` is blocked because it calls `distribute()` internally. [8](#0-7) [2](#0-1)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L718-756)
```text
    /// Distribute any withdrawable stake from the stake pool.
    public entry fun distribute(contract_address: address) acquires VestingContract {
        assert_active_vesting_contract(contract_address);

        let vesting_contract = borrow_global_mut<VestingContract>(contract_address);
        let coins = withdraw_stake(vesting_contract, contract_address);
        let total_distribution_amount = coin::value(&coins);
        if (total_distribution_amount == 0) {
            coin::destroy_zero(coins);
            return
        };

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

        // Send any remaining "dust" (leftover due to rounding error) to the withdrawal address.
        if (coin::value(&coins) > 0) {
            aptos_account::deposit_coins(vesting_contract.withdrawal_address, coins);
        } else {
            coin::destroy_zero(coins);
        };

        emit(
            Distribute {
                admin: vesting_contract.admin,
                vesting_contract_address: contract_address,
                amount: total_distribution_amount,
            },
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L771-775)
```text
    public entry fun terminate_vesting_contract(admin: &signer, contract_address: address) acquires VestingContract {
        assert_active_vesting_contract(contract_address);

        // Distribute all withdrawable coins, which should have been from previous rewards withdrawal or vest.
        distribute(contract_address);
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L95-98)
```text
        recipients.enumerate_ref(|i, to| {
                let amount = amounts[i];
                transfer_coins<CoinType>(from, *to, amount);
            });
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

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.spec.move (L156-157)
```text
        aborts_if exists i in 0..len(recipients):
            exists<coin::CoinStore<AptosCoin>>(recipients[i]) && global<coin::CoinStore<AptosCoin>>(recipients[i]).frozen;
```
