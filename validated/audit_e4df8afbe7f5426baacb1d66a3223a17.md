### Title
Frozen-Store DoS Permanently Blocks `vesting::distribute` and `staking_contract::distribute_internal` for All Shareholders — (`aptos-move/framework/aptos-framework/sources/vesting.move`, `staking_contract.move`)

---

### Summary

`vesting::distribute` and `staking_contract::distribute_internal` iterate over every recipient in a single atomic transaction and call `aptos_account::deposit_coins` for each. If any one recipient's deposit aborts (frozen `CoinStore`, reserved-address beneficiary, or missing store), the entire transaction reverts. Because the coins are re-locked in the stake pool on revert, the distribution can never complete while the blocking condition persists, permanently freezing all shareholders' funds.

---

### Finding Description

**`vesting::distribute`** (lines 718–756, `vesting.move`):

```move
public entry fun distribute(contract_address: address) acquires VestingContract {
    assert_active_vesting_contract(contract_address);
    let vesting_contract = borrow_global_mut<VestingContract>(contract_address);
    let coins = withdraw_stake(vesting_contract, contract_address);
    ...
    shareholders.for_each_ref(|shareholder| {
        let shareholder = *shareholder;
        ...
        let recipient_address = get_beneficiary(vesting_contract, shareholder);
        aptos_account::deposit_coins(recipient_address, share_of_coins); // ← abort here rolls back everything
    });
    ...
}
``` [1](#0-0) 

`aptos_account::deposit_coins` (lines 111–131, `aptos_account.move`) ultimately calls `coin::deposit<CoinType>(to, coins)`, which aborts if `CoinStore<CoinType>` is frozen: [2](#0-1) 

The formal spec confirms the abort condition:

```
spec schema DepositAbortsIf<CoinType> {
    aborts_if !exists<CoinStore<CoinType>>(account_addr);
    aborts_if coin_store.frozen;
}
``` [3](#0-2) 

**`staking_contract::distribute_internal`** (lines 855–920, `staking_contract.move`) has the identical pattern — it loops over all distribution-pool shareholders and calls `aptos_account::deposit_coins` for each, including the operator's beneficiary:

```move
while (distribution_pool.shareholders_count() > 0) {
    ...
    if (recipient == operator) {
        recipient = beneficiary_for_operator(operator);
    };
    aptos_account::deposit_coins(
        recipient, coin::extract(&mut coins, amount_to_distribute)
    );
};
``` [4](#0-3) 

`set_beneficiary_for_operator` (lines 811–838) has **no validation** on `new_beneficiary`: [5](#0-4) 

An operator can set their beneficiary to `@vm_reserved` (`0x0`). When `deposit_coins(@vm_reserved, coins)` is called, `create_account(@vm_reserved)` aborts because `@vm_reserved` is a reserved address: [6](#0-5) 

`terminate_vesting_contract` also calls `distribute()` internally, so a stuck `distribute()` also blocks contract termination: [7](#0-6) 

---

### Impact Explanation

- **`vesting::distribute`**: All shareholders of the vesting contract are permanently unable to receive their vested APT. Because `terminate_vesting_contract` calls `distribute()` first, the admin also cannot terminate the contract or recover funds via `admin_withdraw`. Vested coins remain locked in the stake pool's inactive state indefinitely.
- **`staking_contract::distribute_internal`**: All stakers whose staking contract involves the malicious operator are permanently unable to receive their unlocked stake or commission distributions.

Both constitute **permanent freezing of user-controlled staking/vesting balances**, which is within the allowed impact scope.

---

### Likelihood Explanation

- **`staking_contract` path**: Fully unprivileged. Any operator (when `operator_beneficiary_change_enabled` feature is active on mainnet) can call `set_beneficiary_for_operator` with `@vm_reserved` or any other address that causes `deposit_coins` to abort. No admin or governance action is required.
- **`vesting` path**: Requires the vesting admin to set a beneficiary to a problematic address, or requires the framework to freeze a shareholder's `CoinStore<AptosCoin>`. The former is a privileged action; the latter is a governance action. However, the impact once triggered is irreversible without further admin intervention.

---

### Recommendation

Replace the direct-push distribution pattern with a pull-based (claim) model:

1. In `distribute()` and `distribute_internal()`, instead of calling `deposit_coins` for each recipient atomically, record each recipient's claimable amount in a per-address mapping (e.g., `SimpleMap<address, u64>`).
2. Add a separate `claim(recipient)` entry function that allows each recipient to pull their own funds.
3. This isolates a single recipient's failure (frozen store, bad address) from all other recipients, exactly as the external report recommends.

---

### Proof of Concept

**Attack flow for `staking_contract` (unprivileged):**

1. Operator calls `set_beneficiary_for_operator(operator_signer, @0x0)` — sets beneficiary to `@vm_reserved`.
2. Staker calls `unlock_stake(staker, operator, amount)` — records a distribution for the staker.
3. After lockup expires, anyone calls `distribute(staker_address, operator_address)`.
4. `distribute_internal` loops: when it reaches the operator's share, it resolves `recipient = beneficiary_for_operator(operator) = @0x0`.
5. `aptos_account::deposit_coins(@0x0, coins)` → `create_account(@0x0)` → **aborts** (`@0x0 == @vm_reserved`).
6. Entire transaction reverts. Coins return to stake pool. Distribution is permanently stuck.
7. Staker can never receive their unlocked stake.

**Attack flow for `vesting` (requires frozen store or admin-set bad beneficiary):**

1. Framework freezes shareholder S's `CoinStore<AptosCoin>` (governance action).
2. Anyone calls `vesting::distribute(contract_address)`.
3. Loop reaches shareholder S → `deposit_coins(S, share_of_coins)` → `coin::deposit` → **aborts** (store frozen).
4. Entire transaction reverts. All shareholders permanently blocked. `terminate_vesting_contract` also fails.

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

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L771-793)
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

        emit(
            Terminate {
                admin: vesting_contract.admin,
                vesting_contract_address: contract_address,
            },
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L54-57)
```text
    public entry fun create_account(auth_key: address) {
        let account_signer = account::create_account(auth_key);
        register_apt(&account_signer);
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

**File:** aptos-move/framework/aptos-framework/sources/coin.spec.move (L333-338)
```text
    spec schema DepositAbortsIf<CoinType> {
        account_addr: address;
        let coin_store = global<CoinStore<CoinType>>(account_addr);
        aborts_if !exists<CoinStore<CoinType>>(account_addr);
        aborts_if coin_store.frozen;
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L811-838)
```text
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
