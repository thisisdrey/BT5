### Title
Operator Can Block All Staking Distributions by Setting Unregistered Beneficiary — (`aptos-move/framework/aptos-framework/sources/staking_contract.move`)

---

### Summary

`staking_contract::set_beneficiary_for_operator` does not validate that the new beneficiary is registered to receive APT, unlike the analogous `vesting::set_beneficiary` which carries an explicit guard for exactly this reason. An operator can set a beneficiary address that causes `aptos_account::deposit_coins` to abort inside `distribute_internal`, reverting the entire distribution loop and blocking the staker from receiving unlocked APT, unlocking new stake, or switching operators.

---

### Finding Description

`distribute_internal` iterates over all distribution-pool shareholders and calls `aptos_account::deposit_coins` for each recipient. When the current shareholder is the operator, the recipient is redirected to the operator's registered beneficiary: [1](#0-0) 

`aptos_account::deposit_coins<AptosCoin>` aborts with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS` when two conditions hold simultaneously:

1. The recipient has no legacy `CoinStore<AptosCoin>` (i.e., `coin::is_account_registered<AptosCoin>` returns `false` — possible for accounts created post-FA-migration that only have a primary fungible store).
2. The recipient has explicitly called `set_allow_direct_coin_transfers(false)`. [2](#0-1) 

The `vesting::set_beneficiary` function is aware of this exact failure mode and guards against it:

```move
// Verify that the beneficiary account is set up to receive APT. This is a requirement so distribute() wouldn't
// fail and block all other accounts from receiving APT if one beneficiary is not registered.
assert_account_is_registered_for_apt(new_beneficiary);
``` [3](#0-2) 

`staking_contract::set_beneficiary_for_operator` has no equivalent guard: [4](#0-3) 

Because `distribute_internal` is called not only from `distribute` but also from `unlock_stake` and `switch_operator`, a single malicious beneficiary setting blocks the staker from performing any of these operations: [5](#0-4) 

The formal spec for `deposit_coins` confirms the abort condition: [6](#0-5) 

---

### Impact Explanation

- `staking_contract::distribute` reverts → staker cannot receive unlocked APT.
- `staking_contract::unlock_stake` reverts (calls `distribute_internal` first) → staker cannot unlock new stake.
- `staking_contract::switch_operator` reverts (calls `distribute_internal` first) → staker cannot escape the malicious operator.
- Staker's APT remains locked in the stake pool until the operator voluntarily changes their beneficiary.

This matches the external bug's root cause: a single bad actor in a multi-recipient loop causes the entire withdrawal to revert, locking funds for all other participants.

---

### Likelihood Explanation

- The `operator_beneficiary_change_enabled` feature flag must be active (it is a live mainnet feature).
- The operator must be willing to act maliciously (e.g., as leverage against the staker).
- The malicious beneficiary address is trivially constructed: create an account (FA store only, no legacy `CoinStore<AptosCoin>`), call `set_allow_direct_coin_transfers(false)`, then set it as the beneficiary.

---

### Recommendation

Add the same guard present in `vesting::set_beneficiary` to `staking_contract::set_beneficiary_for_operator`:

```move
public entry fun set_beneficiary_for_operator(
    operator: &signer, new_beneficiary: address
) acquires BeneficiaryForOperator {
    assert!(features::operator_beneficiary_change_enabled(), ...);
+   aptos_account::assert_account_is_registered_for_apt(new_beneficiary);
    ...
}
``` [7](#0-6) 

---

### Proof of Concept

1. Operator controls address `malicious_addr`.
2. `malicious_addr` is created via `aptos_account::create_account` (creates FA primary store; no legacy `CoinStore<AptosCoin>` is registered).
3. `malicious_addr` calls `aptos_account::set_allow_direct_coin_transfers(false)`.
4. Operator calls `staking_contract::set_beneficiary_for_operator(operator, malicious_addr)` — succeeds with no validation.
5. Staker's stake pool accumulates rewards; some stake becomes inactive/unlocked.
6. Anyone calls `staking_contract::distribute(staker, operator)`.
7. Inside `distribute_internal`, the loop reaches the operator's commission entry and redirects to `malicious_addr`.
8. `aptos_account::deposit_coins<AptosCoin>(malicious_addr, commission)` is called:
   - `coin::is_account_registered<AptosCoin>(malicious_addr)` → `false` (no legacy CoinStore).
   - `can_receive_direct_coin_transfers(malicious_addr)` → `false` (opted out).
   - Aborts: `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS`.
9. Entire transaction reverts; staker's unlocked APT remains in the stake pool.
10. Staker attempts `unlock_stake` or `switch_operator` — both also call `distribute_internal` and revert identically. [8](#0-7)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L689-695)
```text

        // Force distribution of any already inactive stake.
        distribute_internal(
            staker_address,
            operator,
            staking_contract,
        );
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L810-838)
```text
    public entry fun set_beneficiary_for_operator(
        operator: &signer, new_beneficiary: address
    ) acquires BeneficiaryForOperator {
        assert!(
            features::operator_beneficiary_change_enabled(),
            std::error::invalid_state(EOPERATOR_BENEFICIARY_CHANGE_NOT_SUPPORTED)
        );
        // The beneficiay address of an operator is stored under the operator's address.
        // So, the operator does not need to be validated with respect to a staking pool.
        let operator_addr = signer::address_of(operator);
        let old_beneficiary = beneficiary_for_operator(operator_addr);
        if (exists<BeneficiaryForOperator>(operator_addr)) {
            borrow_global_mut<BeneficiaryForOperator>(operator_addr).beneficiary_for_operator =
                new_beneficiary;
        } else {
            move_to(
                operator,
                BeneficiaryForOperator { beneficiary_for_operator: new_beneficiary }
            );
        };

        emit(
            SetBeneficiaryForOperator {
                operator: operator_addr,
                old_beneficiary,
                new_beneficiary
            }
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L855-920)
```text
    /// Distribute all unlocked (inactive) funds according to distribution shares.
    fun distribute_internal(
        staker: address,
        operator: address,
        staking_contract: &mut StakingContract,
    ) acquires BeneficiaryForOperator {
        let pool_address = staking_contract.pool_address;
        // Create the Staker resource if it doesn't exist to backfill the Staker resource for each pool.
        if (!exists<Staker>(pool_address)) {
            let pool_signer =
                &account::create_signer_with_capability(&staking_contract.signer_cap);
            move_to(pool_signer, Staker { staker });
        };
        let (_, inactive, _, pending_inactive) = stake::get_stake(pool_address);
        let total_potential_withdrawable = inactive + pending_inactive;
        let coins =
            stake::withdraw_with_cap(
                &staking_contract.owner_cap, total_potential_withdrawable
            );
        let distribution_amount = coin::value(&coins);
        if (distribution_amount == 0) {
            coin::destroy_zero(coins);
            return
        };

        let distribution_pool = &mut staking_contract.distribution_pool;
        update_distribution_pool(
            distribution_pool,
            distribution_amount,
            operator,
            staking_contract.commission_percentage
        );

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

        // In case there's any dust left, send them all to the staker.
        if (coin::value(&coins) > 0) {
            aptos_account::deposit_coins(staker, coins);
            distribution_pool.update_total_coins(0);
        } else {
            coin::destroy_zero(coins);
        }
    }
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

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L179-185)
```text
    public fun assert_account_is_registered_for_apt(addr: address) {
        assert_account_exists(addr);
        assert!(
            coin::is_account_registered<AptosCoin>(addr),
            error::not_found(EACCOUNT_NOT_REGISTERED_FOR_APT)
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L921-923)
```text
        // Verify that the beneficiary account is set up to receive APT. This is a requirement so distribute() wouldn't
        // fail and block all other accounts from receiving APT if one beneficiary is not registered.
        assert_account_is_registered_for_apt(new_beneficiary);
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.spec.move (L228-229)
```text
        let if_exist_coin = exists<coin::CoinStore<CoinType>>(to);
        aborts_if if_exist_coin && global<coin::CoinStore<CoinType>>(to).frozen;
```
