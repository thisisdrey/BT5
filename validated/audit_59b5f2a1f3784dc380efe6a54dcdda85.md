### Title
Operator Can Permanently Freeze Staker Funds via Unvalidated Beneficiary in `staking_contract::set_beneficiary_for_operator` — (File: `aptos-move/framework/aptos-framework/sources/staking_contract.move`)

---

### Summary

`staking_contract::set_beneficiary_for_operator` accepts any arbitrary address as `new_beneficiary` with zero validation. If the operator sets a beneficiary that has explicitly disabled direct coin transfers (`allow_arbitrary_coin_transfers = false`) and has not registered for `AptosCoin`, every subsequent call to `distribute_internal` aborts at `aptos_account::deposit_coins`. Because `distribute_internal` is called unconditionally by every staker-facing withdrawal path (`unlock_stake`, `unlock_rewards`, `switch_operator`, `switch_operator_with_same_commission`, `update_commission_percentage`, `distribute`), the staker's entire staking balance is frozen with no recovery path that does not require the operator's cooperation.

The Aptos team already identified this exact class of bug in `vesting::set_beneficiary` and added `assert_account_is_registered_for_apt(new_beneficiary)` there with the explicit comment: *"This is a requirement so distribute() wouldn't fail and block all other accounts from receiving APT if one beneficiary is not registered."* That guard was never applied to `staking_contract::set_beneficiary_for_operator`.

---

### Finding Description

**Step 1 – No validation in `set_beneficiary_for_operator`**

`staking_contract::set_beneficiary_for_operator` stores any caller-supplied address directly: [1](#0-0) 

There is no call to `assert_account_is_registered_for_apt`, no existence check, and no check of `can_receive_direct_coin_transfers`.

**Step 2 – `distribute_internal` routes operator commission to the beneficiary via `deposit_coins`** [2](#0-1) 

When the operator is a shareholder in the distribution pool, `recipient` is replaced with `beneficiary_for_operator(operator)` and then `aptos_account::deposit_coins` is called on that address.

**Step 3 – `deposit_coins` aborts if the beneficiary has disabled direct transfers** [3](#0-2) 

If the beneficiary account exists, has not registered for `AptosCoin`, and has called `set_allow_direct_coin_transfers(false)`, `can_receive_direct_coin_transfers` returns `false` and the function aborts with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS`. Because Move transactions are atomic, the entire `distribute_internal` call is rolled back.

**Step 4 – Every staker withdrawal path calls `distribute_internal`**

`unlock_stake`, `unlock_rewards`, `switch_operator`, `switch_operator_with_same_commission`, `update_commission_percentage`, and `distribute` all call `distribute_internal` before doing any useful work: [4](#0-3) [5](#0-4) 

Once the operator has a pending distribution (created by `request_commission_internal`) and the beneficiary is invalid, every one of these entry points aborts. The staker has no code path to recover funds without the operator first correcting the beneficiary.

**Step 5 – The fix exists in `vesting.move` but was never applied here**

`vesting::set_beneficiary` explicitly guards against this: [6](#0-5) 

The comment reads: *"This is a requirement so distribute() wouldn't fail and block all other accounts from receiving APT if one beneficiary is not registered."* The identical protection is absent from `staking_contract::set_beneficiary_for_operator` and `delegation_pool::set_beneficiary_for_operator`. [7](#0-6) 

---

### Impact Explanation

A malicious operator can:
1. Set the beneficiary to an address that has disabled direct coin transfers.
2. Call `request_commission` to create a pending distribution entry for the operator in the distribution pool.
3. From that point forward, every staker-initiated withdrawal, operator-switch, or commission-update transaction aborts.

The staker's entire staking balance — principal plus accumulated rewards — is frozen for as long as the operator refuses to correct the beneficiary. Because `switch_operator` also calls `distribute_internal` first, the staker cannot escape to a new operator without the current operator's cooperation. This satisfies the "permanent freezing of staking balances" impact criterion.

---

### Likelihood Explanation

The operator is an unprivileged on-chain account, not a governance admin. Any operator who has been assigned a staking contract can execute this attack in two transactions. The precondition (a beneficiary address with `allow_arbitrary_coin_transfers = false` and no `AptosCoin` registration) is trivially constructable: create a fresh account, call `set_allow_direct_coin_transfers(false)`, and never register for `AptosCoin`. The attack is cheap, reversible by the attacker at will (making it useful for extortion), and leaves no on-chain evidence beyond a beneficiary address change event.

---

### Recommendation

Apply the same guard that `vesting::set_beneficiary` already uses:

```move
public entry fun set_beneficiary_for_operator(
    operator: &signer, new_beneficiary: address
) acquires BeneficiaryForOperator {
    assert!(
        features::operator_beneficiary_change_enabled(),
        std::error::invalid_state(EOPERATOR_BENEFICIARY_CHANGE_NOT_SUPPORTED)
    );
+   // Ensure the beneficiary can receive APT so distribute_internal cannot be blocked.
+   aptos_account::assert_account_is_registered_for_apt(new_beneficiary);
    ...
}
```

Apply the same fix to `delegation_pool::set_beneficiary_for_operator`.

---

### Proof of Concept

```
// Setup: attacker controls `operator` account and `bad_addr` account.

// 1. bad_addr disables direct coin transfers and never registers for AptosCoin.
aptos_account::set_allow_direct_coin_transfers(&bad_addr_signer, false);
// (bad_addr never calls coin::register<AptosCoin>)

// 2. Operator redirects commission to bad_addr.
staking_contract::set_beneficiary_for_operator(&operator_signer, bad_addr);

// 3. Operator creates a pending distribution entry.
staking_contract::request_commission(&operator_signer, staker_addr, operator_addr);
// → commission_amount moved to pending_inactive in the stake pool
// → operator now has shares in distribution_pool

// 4. After lockup expires, staker tries to withdraw.
staking_contract::unlock_stake(&staker_signer, operator_addr, amount);
// → calls distribute_internal
// → distribute_internal calls aptos_account::deposit_coins(bad_addr, commission_coins)
// → deposit_coins: bad_addr exists, not registered for AptosCoin,
//   can_receive_direct_coin_transfers(bad_addr) == false
// → ABORT: EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS
// Staker's funds remain locked.

// 5. Staker tries to escape by switching operators.
staking_contract::switch_operator(&staker_signer, operator_addr, new_operator_addr, 10);
// → calls distribute_internal first → same abort.
// Staker is permanently stuck.
```

### Citations

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L690-695)
```text
        // Force distribution of any already inactive stake.
        distribute_internal(
            staker_address,
            operator,
            staking_contract,
        );
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L783-789)
```text
        let (_, staking_contract) = staking_contracts.remove(&old_operator);
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

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L895-901)
```text
            // If the recipient is the operator, send the commission to the beneficiary instead.
            if (recipient == operator) {
                recipient = beneficiary_for_operator(operator);
            };
            aptos_account::deposit_coins(
                recipient, coin::extract(&mut coins, amount_to_distribute)
            );
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

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L921-923)
```text
        // Verify that the beneficiary account is set up to receive APT. This is a requirement so distribute() wouldn't
        // fail and block all other accounts from receiving APT if one beneficiary is not registered.
        assert_account_is_registered_for_apt(new_beneficiary);
```

**File:** aptos-move/framework/aptos-framework/sources/delegation_pool.move (L1272-1291)
```text
    public entry fun set_beneficiary_for_operator(
        operator: &signer,
        new_beneficiary: address
    ) acquires BeneficiaryForOperator {
        // The beneficiay address of an operator is stored under the operator's address.
        // So, the operator does not need to be validated with respect to a staking pool.
        let operator_addr = signer::address_of(operator);
        let old_beneficiary = beneficiary_for_operator(operator_addr);
        if (exists<BeneficiaryForOperator>(operator_addr)) {
            borrow_global_mut<BeneficiaryForOperator>(operator_addr).beneficiary_for_operator = new_beneficiary;
        } else {
            move_to(operator, BeneficiaryForOperator { beneficiary_for_operator: new_beneficiary });
        };

        emit(SetBeneficiaryForOperator {
            operator: operator_addr,
            old_beneficiary,
            new_beneficiary,
        });
    }
```
