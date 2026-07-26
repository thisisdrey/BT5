### Title
Sequential Distribution Loop DoS via Unregistered Recipient — (`aptos-move/framework/aptos-framework/sources/vesting.move`)

### Summary

`vesting::distribute()` iterates over every shareholder in a single atomic transaction and calls `aptos_account::deposit_coins` for each one. If any single call aborts, the entire transaction reverts and **no** shareholder receives their distribution. The same structural flaw exists in `staking_contract::distribute_internal()`. The code itself acknowledges the risk for the beneficiary path but leaves the original shareholder path unguarded.

---

### Finding Description

**Primary path — `vesting::distribute()`** [1](#0-0) 

The loop calls `aptos_account::deposit_coins(recipient_address, share_of_coins)` for every shareholder without any error isolation. `deposit_coins` aborts with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS` when the recipient account is **not** registered for `AptosCoin` **and** has called `set_allow_direct_coin_transfers(false)`: [2](#0-1) 

`set_allow_direct_coin_transfers` is a public, unprivileged entry function any account holder can call on their own address: [3](#0-2) 

`create_vesting_contract` validates the `withdrawal_address` for APT registration but performs **no equivalent check on shareholder addresses**: [4](#0-3) 

The code comment inside `set_beneficiary` explicitly acknowledges the DoS risk and adds a guard there — but only there: [5](#0-4) 

The same guard is absent for the original shareholder list supplied at contract creation.

**Secondary path — `staking_contract::distribute_internal()`** [6](#0-5) 

The `while` loop redeems every shareholder from the distribution pool and calls `deposit_coins` for each. If the operator's beneficiary (set via `set_beneficiary_for_operator`, an unprivileged operator action) is an address not registered for APT with direct transfers disabled, the loop aborts and the staker's principal distribution is also blocked.

---

### Impact Explanation

A single shareholder whose address is not registered for `AptosCoin` and who has called `set_allow_direct_coin_transfers(false)` causes every call to `vesting::distribute()` on that contract to revert. All other shareholders are permanently unable to receive their vested APT until the admin intervenes by setting a beneficiary for the problematic shareholder. During that window, vesting balances are effectively frozen — matching the "permanent freezing of vesting balances" impact class in scope.

---

### Likelihood Explanation

- `create_vesting_contract` accepts any address as a shareholder without checking APT registration. An employee account that exists on-chain but has never registered for APT (e.g., a freshly created resource account) is a realistic shareholder.
- The shareholder then calls `set_allow_direct_coin_transfers(false)` — a single, zero-cost, unprivileged transaction — to arm the DoS.
- `distribute()` is callable by anyone, so the DoS is triggered on the next distribution attempt.
- The admin can remediate by calling `set_beneficiary` for the problematic shareholder, but `set_beneficiary` itself requires the new beneficiary to be registered for APT, so the admin must coordinate with a third party or use a pre-registered address.

---

### Recommendation

1. In `create_vesting_contract`, add `assert_account_is_registered_for_apt(*shareholder)` for every shareholder address, mirroring the existing check on `withdrawal_address`.
2. In `vesting::distribute()`, wrap each `deposit_coins` call in a try/skip pattern (or send failed distributions to the `withdrawal_address`) so one bad recipient cannot block the rest.
3. Apply the same fix to `staking_contract::distribute_internal()` — either validate beneficiary registration in `set_beneficiary_for_operator` or make the distribution loop resilient to individual failures.

---

### Proof of Concept

```
1. Admin calls create_vesting_contract with shareholders = [@alice, @bob]
   where @alice exists on-chain but has never called coin::register<AptosCoin>.
   No error — create_vesting_contract does not check shareholder registration.

2. @alice calls:
     aptos_account::set_allow_direct_coin_transfers(alice_signer, false)
   Cost: one transaction, no privilege required.

3. Vesting period elapses. Anyone calls:
     vesting::distribute(contract_address)

4. Inside distribute():
     for shareholder in [alice, bob]:
         deposit_coins(alice_address, alice_share)   // aborts here
         ...

   deposit_coins checks:
     coin::is_account_registered<AptosCoin>(alice) == false  // never registered
     can_receive_direct_coin_transfers(alice)        == false // step 2
     → abort EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS

5. Entire transaction reverts. @bob receives nothing.
   distribute() will continue to revert on every future call
   until the admin sets a beneficiary for @alice.
``` [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L537-576)
```text
    public fun create_vesting_contract(
        admin: &signer,
        shareholders: &vector<address>,
        buy_ins: SimpleMap<address, Coin<AptosCoin>>,
        vesting_schedule: VestingSchedule,
        withdrawal_address: address,
        operator: address,
        voter: address,
        commission_percentage: u64,
        // Optional seed used when creating the staking contract account.
        contract_creation_seed: vector<u8>,
    ): address acquires AdminStore {
        assert!(
            !system_addresses::is_reserved_address(withdrawal_address),
            error::invalid_argument(EINVALID_WITHDRAWAL_ADDRESS),
        );
        assert_account_is_registered_for_apt(withdrawal_address);
        assert!(shareholders.length() > 0, error::invalid_argument(ENO_SHAREHOLDERS));
        assert!(
            buy_ins.length() == shareholders.length(),
            error::invalid_argument(ESHARES_LENGTH_MISMATCH),
        );

        // Create a coins pool to track shareholders and shares of the grant.
        let grant = coin::zero<AptosCoin>();
        let grant_amount = 0;
        let grant_pool = pool_u64::create(MAXIMUM_SHAREHOLDERS);
        shareholders.for_each_ref(|shareholder| {
            let shareholder: address = *shareholder;
            let (_, buy_in) = simple_map::remove(&mut buy_ins, &shareholder);
            let buy_in_amount = coin::value(&buy_in);
            coin::merge(&mut grant, buy_in);
            pool_u64::buy_in(
                &mut grant_pool,
                shareholder,
                buy_in_amount,
            );
            grant_amount += buy_in_amount;
        });
        assert!(grant_amount > 0, error::invalid_argument(EZERO_GRANT));
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L719-756)
```text
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

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L921-923)
```text
        // Verify that the beneficiary account is set up to receive APT. This is a requirement so distribute() wouldn't
        // fail and block all other accounts from receiving APT if one beneficiary is not registered.
        assert_account_is_registered_for_apt(new_beneficiary);
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
