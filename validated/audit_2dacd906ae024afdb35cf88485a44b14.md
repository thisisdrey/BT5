### Title
Operator-Controlled Beneficiary Without Validation Permanently Blocks Vesting Distribution, Locking All Shareholders' APT — (`aptos-move/framework/aptos-framework/sources/vesting.move`, `staking_contract.move`)

---

### Summary

`staking_contract::set_beneficiary_for_operator` accepts any arbitrary address as the operator's commission beneficiary with no validation. If the operator sets a beneficiary that has disabled direct coin transfers (`allow_arbitrary_coin_transfers = false`) and is not registered for APT, then `aptos_account::deposit_coins` aborts inside `distribute_internal`. Because `vesting::distribute` calls `withdraw_stake → staking_contract::distribute → distribute_internal` atomically, and because `staking_contract::switch_operator` also calls `distribute_internal` before switching, the entire vesting contract becomes permanently stuck: no shareholder can ever receive their vested APT, and the admin cannot rotate the operator to recover.

---

### Finding Description

**The guarded path (shareholder beneficiaries):**

`vesting::set_beneficiary` explicitly validates the new beneficiary before storing it:

```move
// vesting.move line 923
assert_account_is_registered_for_apt(new_beneficiary);
```

The inline comment even states the reason: *"This is a requirement so distribute() wouldn't fail and block all other accounts from receiving APT if one beneficiary is not registered."* [1](#0-0) 

**The unguarded path (operator beneficiary):**

`staking_contract::set_beneficiary_for_operator` stores any arbitrary address with zero validation:

```move
// staking_contract.move lines 810-838
public entry fun set_beneficiary_for_operator(
    operator: &signer, new_beneficiary: address
) acquires BeneficiaryForOperator {
    ...
    move_to(operator, BeneficiaryForOperator { beneficiary_for_operator: new_beneficiary });
    ...
}
``` [2](#0-1) 

**The push that fails:**

Inside `distribute_internal`, when the operator's share is distributed, the code redirects to `beneficiary_for_operator(operator)` and calls `aptos_account::deposit_coins`:

```move
// staking_contract.move lines 895-901
if (recipient == operator) {
    recipient = beneficiary_for_operator(operator);
};
aptos_account::deposit_coins(
    recipient, coin::extract(&mut coins, amount_to_distribute)
);
``` [3](#0-2) 

`aptos_account::deposit_coins` aborts if the recipient is not registered for the CoinType AND has disabled direct transfers:

```move
// aptos_account.move lines 123-127
if (!coin::is_account_registered<CoinType>(to)) {
    assert!(
        can_receive_direct_coin_transfers(to),
        error::permission_denied(EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS)
    );
``` [4](#0-3) 

`can_receive_direct_coin_transfers` returns `false` when the account has explicitly called `set_allow_direct_coin_transfers(false)`:

```move
// aptos_account.move lines 228-231
public fun can_receive_direct_coin_transfers(account: address): bool acquires DirectTransferConfig {
    !exists<DirectTransferConfig>(account)
        || borrow_global<DirectTransferConfig>(account).allow_arbitrary_coin_transfers
}
``` [5](#0-4) 

**The call chain that propagates the abort:**

`vesting::distribute` calls `withdraw_stake`, which calls `staking_contract::distribute`, which calls `distribute_internal`:

```move
// vesting.move lines 722-723
let vesting_contract = borrow_global_mut<VestingContract>(contract_address);
let coins = withdraw_stake(vesting_contract, contract_address);
``` [6](#0-5) 

```move
// vesting.move lines 1071-1078
fun withdraw_stake(vesting_contract: &VestingContract, contract_address: address): Coin<AptosCoin> {
    staking_contract::distribute(contract_address, vesting_contract.staking.operator);
    let withdrawn_coins = coin::balance<AptosCoin>(contract_address);
    let contract_signer = &get_vesting_account_signer_internal(vesting_contract);
    coin::withdraw<AptosCoin>(contract_signer, withdrawn_coins)
}
``` [7](#0-6) 

**Why the admin cannot recover:**

`vesting::update_operator` calls `staking_contract::switch_operator`, which calls `distribute_internal` **before** switching the operator:

```move
// staking_contract.move lines 783-789
let (_, staking_contract) = staking_contracts.remove(&old_operator);
// Force distribution of any already inactive stake.
distribute_internal(
    staker_address,
    old_operator,
    &mut staking_contract,
);
``` [8](#0-7) 

If `distribute_internal` aborts (because the operator's beneficiary is a poison address), `switch_operator` also aborts. The admin has no path to rotate the operator and unblock the contract.

---

### Impact Explanation

All shareholders in the vesting contract are permanently unable to receive their vested APT. The `distribute()` entry point is the only mechanism to deliver vested tokens to shareholders, and it is completely blocked. The funds remain locked in the staking pool indefinitely. This constitutes permanent freezing of user-controlled staking/vesting balances — a direct match to the allowed impact gate.

---

### Likelihood Explanation

The operator is a semi-trusted participant (chosen by the vesting admin), but the attack requires only a single unprivileged transaction (`set_beneficiary_for_operator`) callable by the operator at any time after the contract is created. The operator has a financial incentive to do this if they want to hold the vesting contract hostage (e.g., to extract concessions from the admin). The precondition — creating an account with `allow_arbitrary_coin_transfers = false` and no APT registration — is trivially achievable on-chain. Likelihood is Low-to-Medium.

---

### Recommendation

Apply the same validation to `staking_contract::set_beneficiary_for_operator` that `vesting::set_beneficiary` already applies to shareholder beneficiaries:

```move
public entry fun set_beneficiary_for_operator(
    operator: &signer, new_beneficiary: address
) acquires BeneficiaryForOperator {
    // Add this guard, mirroring vesting::set_beneficiary line 923:
    aptos_account::assert_account_is_registered_for_apt(new_beneficiary);
    ...
}
```

Additionally, consider adopting a pull-over-push pattern for commission distribution: store the operator's pending commission in a claimable balance rather than pushing it atomically during `distribute_internal`. This eliminates the entire class of push-failure DoS.

---

### Proof of Concept

```
1. Admin creates a vesting contract with shareholder S and operator O.
2. Operator O creates a fresh account P, calls:
       aptos_account::set_allow_direct_coin_transfers(P_signer, false)
   (P is now a "poison" address: exists, not registered for APT, rejects direct transfers)
3. Operator O calls:
       staking_contract::set_beneficiary_for_operator(O_signer, P)
   No validation occurs; P is stored as O's beneficiary.
4. Stake pool earns rewards. O calls request_commission() — commission enters distribution_pool.
5. Anyone calls vesting::distribute(contract_address):
       → withdraw_stake()
       → staking_contract::distribute()
       → distribute_internal()
       → recipient = beneficiary_for_operator(O) = P
       → aptos_account::deposit_coins(P, commission_coins)
       → coin::is_account_registered<AptosCoin>(P) == false
       → can_receive_direct_coin_transfers(P) == false
       → ABORT: EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS
6. distribute() reverts. Shareholder S receives nothing.
7. Admin tries vesting::update_operator() → staking_contract::switch_operator()
       → distribute_internal() → same abort. Admin is blocked.
8. Vesting contract is permanently stuck. All of S's vested APT is frozen.
``` [9](#0-8) [10](#0-9)

### Citations

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

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L1071-1078)
```text
    fun withdraw_stake(vesting_contract: &VestingContract, contract_address: address): Coin<AptosCoin> {
        // Claim any withdrawable distribution from the staking contract. The withdrawn coins will be sent directly to
        // the vesting contract's account.
        staking_contract::distribute(contract_address, vesting_contract.staking.operator);
        let withdrawn_coins = coin::balance<AptosCoin>(contract_address);
        let contract_signer = &get_vesting_account_signer_internal(vesting_contract);
        coin::withdraw<AptosCoin>(contract_signer, withdrawn_coins)
    }
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

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L123-127)
```text
        if (!coin::is_account_registered<CoinType>(to)) {
            assert!(
                can_receive_direct_coin_transfers(to),
                error::permission_denied(EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS)
            );
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
