### Title
Missing Zero-Address Validation in `set_beneficiary_for_operator` Permanently Freezes Staker Funds — (`aptos-move/framework/aptos-framework/sources/staking_contract.move`)

---

### Summary

`staking_contract::set_beneficiary_for_operator` accepts `@0x0` as `new_beneficiary` without any validation. Once stored, every subsequent call to `distribute_internal` — which is invoked by `distribute`, `request_commission`, `unlock_stake`, and `switch_operator` — attempts `aptos_account::deposit_coins(@0x0, ...)`. That call internally invokes `account::create_account(@0x0)`, which aborts because `@0x0` is the reserved `@vm_reserved` address. The abort propagates up and permanently blocks all fund withdrawals for the staker whose pool uses that operator.

---

### Finding Description

`staking_contract::set_beneficiary_for_operator` stores an arbitrary address with no sanity check: [1](#0-0) 

When `distribute_internal` runs, it redirects the operator's commission share to `beneficiary_for_operator(operator)`: [2](#0-1) 

`aptos_account::deposit_coins` creates the account if it does not exist: [3](#0-2) 

The formal spec for `aptos_account` explicitly states that `@0x0` (`@vm_reserved`) is a reserved address and that `create_account` aborts for it: [4](#0-3) 

The batch-transfer spec confirms the abort condition: [5](#0-4) 

`distribute_internal` is called from every staker-facing withdrawal path: [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) 

The same missing check exists in `delegation_pool::set_beneficiary_for_operator`: [10](#0-9) 

---

### Impact Explanation

Once an operator calls `set_beneficiary_for_operator(@0x0)`, every transaction that touches `distribute_internal` for that staking contract aborts unconditionally. The staker loses the ability to call `unlock_stake`, `distribute`, `request_commission`, or `switch_operator`. Their staked APT is permanently frozen inside the pool with no recovery path, because every exit route goes through `distribute_internal`.

---

### Likelihood Explanation

The operator is a regular on-chain participant (not a governance admin). The entry function is publicly callable by any signer who holds an operator role. A malicious operator can deliberately pass `@0x0` to grief a staker, or an honest operator can do so accidentally (e.g., a client-side bug that serializes a missing address as the zero address). No special privilege beyond holding the operator role is required.

---

### Recommendation

Add a zero-address guard at the top of `set_beneficiary_for_operator` in both `staking_contract.move` and `delegation_pool.move`:

```move
assert!(
    new_beneficiary != @0x0,
    error::invalid_argument(EINVALID_BENEFICIARY_ADDRESS)
);
```

This mirrors the fix applied in the referenced Solidity audit (PR 169 adding a sanity check inside `CoverageFundAddress.set`).

---

### Proof of Concept

1. Operator calls `staking_contract::set_beneficiary_for_operator(operator_signer, @0x0)`.
   - Succeeds; `BeneficiaryForOperator { beneficiary_for_operator: @0x0 }` is stored under `operator_addr`.

2. Staker calls `staking_contract::unlock_stake(staker, operator_addr, amount)`.
   - Internally calls `distribute_internal(staker_addr, operator_addr, staking_contract)`.
   - `distribute_internal` calls `beneficiary_for_operator(operator_addr)` → returns `@0x0`.
   - Calls `aptos_account::deposit_coins(@0x0, commission_coins)`.
   - `account::exists_at(@0x0)` is `false`.
   - `aptos_account::create_account(@0x0)` → `account::create_account(@0x0)` aborts with `EADDRESS_IS_RESERVED` (or equivalent) because `@0x0 == @vm_reserved`.
   - Entire transaction aborts.

3. Staker retries `distribute`, `request_commission`, `switch_operator` — all abort for the same reason.

4. Staker's APT is permanently locked in the stake pool with no withdrawal path.

### Citations

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L607-635)
```text
    public entry fun request_commission(
        account: &signer, staker: address, operator: address
    ) acquires Store, BeneficiaryForOperator {
        let account_addr = signer::address_of(account);
        assert!(
            account_addr == staker
                || account_addr == operator
                || account_addr == beneficiary_for_operator(operator),
            error::unauthenticated(ENOT_STAKER_OR_OPERATOR_OR_BENEFICIARY)
        );
        assert_staking_contract_exists(staker, operator);

        let store = borrow_global_mut<Store>(staker);
        let staking_contract = store.staking_contracts.borrow_mut(&operator);
        // Short-circuit if zero commission.
        if (staking_contract.commission_percentage == 0) { return };

        // Force distribution of any already inactive stake.
        distribute_internal(
            staker,
            operator,
            staking_contract,
        );

        request_commission_internal(
            operator,
            staking_contract,
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L678-695)
```text
    public entry fun unlock_stake(
        staker: &signer, operator: address, amount: u64
    ) acquires Store, BeneficiaryForOperator {
        // Short-circuit if amount is 0.
        if (amount == 0) return;

        let staker_address = signer::address_of(staker);
        assert_staking_contract_exists(staker_address, operator);

        let store = borrow_global_mut<Store>(staker_address);
        let staking_contract = store.staking_contracts.borrow_mut(&operator);

        // Force distribution of any already inactive stake.
        distribute_internal(
            staker_address,
            operator,
            staking_contract,
        );
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L783-796)
```text
        let (_, staking_contract) = staking_contracts.remove(&old_operator);
        // Force distribution of any already inactive stake.
        distribute_internal(
            staker_address,
            old_operator,
            &mut staking_contract,
        );

        // For simplicity, we request commission to be paid out first. This avoids having to ensure to staker doesn't
        // withdraw into the commission portion.
        request_commission_internal(
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

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L842-853)
```text
    public entry fun distribute(
        staker: address, operator: address
    ) acquires Store, BeneficiaryForOperator {
        assert_staking_contract_exists(staker, operator);
        let store = borrow_global_mut<Store>(staker);
        let staking_contract = store.staking_contracts.borrow_mut(&operator);
        distribute_internal(
            staker,
            operator,
            staking_contract,
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

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.spec.move (L1-10)
```text
spec aptos_framework::aptos_account {
    /// <high-level-req>
    /// No.: 1
    /// Requirement: During the creation of an Aptos account the following rules should hold: (1) the authentication key
    /// should be 32 bytes in length, (2) an Aptos account should not already exist for that authentication key, and (3)
    /// the address of the authentication key should not be equal to a reserved address (0x0, 0x1, or 0x3).
    /// Criticality: Critical
    /// Implementation: The authentication key which is passed in as an argument to create_account should satisfy all
    /// necessary conditions.
    /// Enforcement: Formally verified via [high-level-req-1](CreateAccountAbortsIf).
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.spec.move (L139-145)
```text
        aborts_if exists i in 0..len(recipients):
                !account::spec_exists_at(recipients[i]) && length_judgment(recipients[i]);
        aborts_if exists i in 0..len(recipients):
                !account::spec_exists_at(recipients[i]) && (recipients[i] == @vm_reserved || recipients[i] == @aptos_framework || recipients[i] == @aptos_token);
        ensures forall i in 0..len(recipients):
                (!account::spec_exists_at(recipients[i]) ==> !length_judgment(recipients[i])) &&
                    (!account::spec_exists_at(recipients[i]) ==> (recipients[i] != @vm_reserved && recipients[i] != @aptos_framework && recipients[i] != @aptos_token));
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
