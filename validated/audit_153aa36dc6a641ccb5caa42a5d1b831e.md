### Title
`staking_contract::set_beneficiary_for_operator` Missing APT Registration Check Causes Permanent DoS on `distribute_internal` — (`File: aptos-move/framework/aptos-framework/sources/staking_contract.move`)

---

### Summary

`staking_contract::set_beneficiary_for_operator` accepts any arbitrary address as the new beneficiary without verifying that the address can receive APT. When `distribute_internal` later calls `aptos_account::deposit_coins` to pay the operator's commission to that beneficiary, the call aborts if the beneficiary account has opted out of direct coin transfers. This abort propagates to the entire distribution loop, permanently blocking all staking distributions — including the staker's own funds — for every staker using that operator.

---

### Finding Description

`vesting::set_beneficiary` explicitly guards against this exact scenario with the comment:

> *"Verify that the beneficiary account is set up to receive APT. This is a requirement so distribute() wouldn't fail and block all other accounts from receiving APT if one beneficiary is not registered."* [1](#0-0) 

The analogous function `staking_contract::set_beneficiary_for_operator` has no such guard: [2](#0-1) 

Inside `distribute_internal`, when the recipient is the operator, the code redirects payment to `beneficiary_for_operator(operator)` and immediately calls `aptos_account::deposit_coins`: [3](#0-2) 

`aptos_account::deposit_coins` aborts with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS` when the target account exists, is not registered for `AptosCoin`, and has `allow_arbitrary_coin_transfers = false`: [4](#0-3) 

Because the abort happens inside the distribution loop, the staker's share is never paid out either. Additionally, `switch_operator` calls `distribute_internal` as its first step: [5](#0-4) 

This means the staker cannot even switch to a new operator to recover funds while the malicious beneficiary is set.

---

### Impact Explanation

All APT staking balances held in the staking contract for every staker paired with the affected operator become permanently inaccessible until the operator voluntarily changes their beneficiary. The staker cannot call `distribute`, `unlock_stake`, or `switch_operator` — all three paths invoke `distribute_internal`. This constitutes a permanent freeze of user-controlled staking balances, which is within the Aptos bounty's allowed impact scope.

---

### Likelihood Explanation

The trigger is a single unprivileged transaction: any operator calls `set_beneficiary_for_operator` with an address that has previously called `set_allow_direct_coin_transfers(false)`. No admin access, no governance, no special permissions are required. The operator may do this maliciously or accidentally. The feature flag `operator_beneficiary_change_enabled` is the only prerequisite, and it is enabled on mainnet.

---

### Recommendation

Add the same guard that `vesting::set_beneficiary` uses:

```move
public entry fun set_beneficiary_for_operator(
    operator: &signer, new_beneficiary: address
) acquires BeneficiaryForOperator {
    assert!(
        features::operator_beneficiary_change_enabled(),
        std::error::invalid_state(EOPERATOR_BENEFICIARY_CHANGE_NOT_SUPPORTED)
    );
+   // Verify the beneficiary can receive APT so distribute() cannot be blocked.
+   aptos_account::assert_account_is_registered_for_apt(new_beneficiary);
    ...
}
``` [6](#0-5) 

---

### Proof of Concept

```
1. Account X calls:
       aptos_account::set_allow_direct_coin_transfers(X_signer, false)
   X now has DirectTransferConfig { allow_arbitrary_coin_transfers: false }
   and is NOT registered for AptosCoin.

2. Operator calls:
       staking_contract::set_beneficiary_for_operator(operator_signer, X)
   No validation — succeeds.

3. Anyone calls:
       staking_contract::distribute(staker_addr, operator_addr)

   Inside distribute_internal:
     recipient = operator_addr
     recipient = beneficiary_for_operator(operator_addr)  // → X
     aptos_account::deposit_coins(X, commission_coins)
       → account::exists_at(X) == true  (skip create_account)
       → coin::is_account_registered<AptosCoin>(X) == false
       → can_receive_direct_coin_transfers(X) == false
       → ABORT: EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS

4. distribute() aborts. Staker's funds are never paid out.

5. staker calls switch_operator(staker_signer, operator_addr, new_operator, pct)
   → distribute_internal called at line 785 → same abort.
   Staker is fully locked out.
``` [7](#0-6) [8](#0-7)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L921-923)
```text
        // Verify that the beneficiary account is set up to receive APT. This is a requirement so distribute() wouldn't
        // fail and block all other accounts from receiving APT if one beneficiary is not registered.
        assert_account_is_registered_for_apt(new_beneficiary);
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L784-789)
```text
        // Force distribution of any already inactive stake.
        distribute_internal(
            staker_address,
            old_operator,
            &mut staking_contract,
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

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L856-920)
```text
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
