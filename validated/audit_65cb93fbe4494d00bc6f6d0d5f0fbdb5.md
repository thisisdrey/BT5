### Title
Unbounded `AdminStore.vesting_contracts` Vector with No Removal Mechanism Causes Permanent DoS on `staking_proxy` Entry Functions — (`aptos-move/framework/aptos-framework/sources/vesting.move`)

### Summary

The `AdminStore` resource in `vesting.move` stores a `vector<address>` called `vesting_contracts` that grows by one entry every time `create_vesting_contract` is called. No code path — including `terminate_vesting_contract` or `admin_withdraw` — ever removes an entry from this vector. The `staking_proxy` module's entry functions `set_vesting_contract_operator` and `set_vesting_contract_voter` iterate over the entire vector on every call. Once the vector is large enough, these entry functions permanently exceed the per-transaction gas limit, locking the admin out of operator and voter management for all their vesting contracts.

### Finding Description

**Root cause — unbounded push with no pop:**

`AdminStore` is defined as:

```move
struct AdminStore has key {
    vesting_contracts: vector<address>,
    nonce: u64,
    create_events: EventHandle<CreateVestingContractEvent>,
}
``` [1](#0-0) 

Every call to `create_vesting_contract` appends to this vector:

```move
admin_store.vesting_contracts.push_back(contract_address);
``` [2](#0-1) 

`terminate_vesting_contract` only sets `state = VESTING_POOL_TERMINATED` and zeroes `remaining_grant`; it never removes the contract address from `AdminStore.vesting_contracts`: [3](#0-2) 

`admin_withdraw` similarly never touches the vector: [4](#0-3) 

**Iteration over the full vector in production entry functions:**

`staking_proxy::set_vesting_contract_operator` fetches the full vector and iterates every element:

```move
let vesting_contracts = &vesting::vesting_contracts(owner_address);
vesting_contracts.for_each_ref(|vesting_contract| {
    ...
    vesting::update_operator(owner, vesting_contract, new_operator, ...);
});
``` [5](#0-4) 

`staking_proxy::set_vesting_contract_voter` has the identical pattern: [6](#0-5) 

Both are `public entry fun` callable by any admin. Each iteration reads a `VestingContract` resource and may call `update_operator` (which itself calls into `staking_contract::switch_operator`), making the per-element cost non-trivial.

The `vesting_contracts` view function also returns the full vector by value (copy), which itself becomes expensive as the vector grows: [7](#0-6) 

### Impact Explanation

Once an admin's `vesting_contracts` vector is large enough that iterating it exceeds the Aptos per-transaction gas limit, the admin permanently loses the ability to:

1. Switch operators via `staking_proxy::set_operator` / `set_vesting_contract_operator` — meaning a misbehaving or offline operator cannot be replaced, and staking rewards/principal for all shareholders in every vesting contract under that admin are at risk.
2. Update voters via `staking_proxy::set_voter` / `set_vesting_contract_voter` — governance voting rights for all associated stake pools become frozen.

Terminated contracts are never pruned, so the vector only ever grows. The admin cannot work around this because there is no partial-iteration or pagination entry point in `staking_proxy`.

### Likelihood Explanation

An admin who creates many vesting contracts over time (e.g., an employee vesting program with many cohorts, each as a separate contract) will accumulate entries indefinitely. Because each `create_vesting_contract` call requires real APT stake, the growth is capital-gated but not prevented. The genesis flow (`genesis.move`) already demonstrates the pattern of creating one contract per employee group: [8](#0-7) 

Over the lifetime of a large vesting program, the vector can grow to a size that makes `set_vesting_contract_operator` and `set_vesting_contract_voter` permanently unexecutable.

### Recommendation

In `terminate_vesting_contract` (and optionally `admin_withdraw`), remove the terminated contract's address from `AdminStore.vesting_contracts` using `swap_remove` for O(1) removal:

```move
// After setting state = VESTING_POOL_TERMINATED:
let admin_store = borrow_global_mut<AdminStore>(vesting_contract.admin);
let (found, idx) = admin_store.vesting_contracts.index_of(&contract_address);
if (found) {
    admin_store.vesting_contracts.swap_remove(idx);
};
```

Alternatively, add a cap on the number of vesting contracts per admin (similar to `MAXIMUM_SHAREHOLDERS = 30` already used for the grant pool): [9](#0-8) 

### Proof of Concept

1. Admin `A` calls `create_vesting_contract` N times (each with minimum stake), accumulating N entries in `AdminStore.vesting_contracts`.
2. Admin `A` calls `terminate_vesting_contract` on all N contracts — the vector remains at length N.
3. Admin `A` calls `staking_proxy::set_vesting_contract_operator(A, old_op, new_op)`.
4. The Move VM iterates all N entries, loading each `VestingContract` resource and calling `vesting::update_operator` for matching ones. At sufficiently large N, the transaction aborts with out-of-gas.
5. The admin can never again call `set_vesting_contract_operator` or `set_vesting_contract_voter`, permanently losing operator/voter management for any future active contracts also stored in the same vector.

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L96-97)
```text
    /// Maximum number of shareholders a vesting pool can support.
    const MAXIMUM_SHAREHOLDERS: u64 = 30;
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L167-173)
```text
    struct AdminStore has key {
        vesting_contracts: vector<address>,
        // Used to create resource accounts for new vesting contracts so there's no address collision.
        nonce: u64,

        create_events: EventHandle<CreateVestingContractEvent>,
    }
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L402-410)
```text
    #[view]
    /// Return all the vesting contracts a given address is an admin of.
    public fun vesting_contracts(admin: address): vector<address> acquires AdminStore {
        if (!exists<AdminStore>(admin)) {
            vector::empty<address>()
        } else {
            borrow_global<AdminStore>(admin).vesting_contracts
        }
    }
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L596-597)
```text
        let admin_store = borrow_global_mut<AdminStore>(admin_address);
        admin_store.vesting_contracts.push_back(contract_address);
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L770-793)
```text
    /// Terminate the vesting contract and send all funds back to the withdrawal address.
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

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L797-821)
```text
    public entry fun admin_withdraw(admin: &signer, contract_address: address) acquires VestingContract {
        let vesting_contract = borrow_global<VestingContract>(contract_address);
        assert!(
            vesting_contract.state == VESTING_POOL_TERMINATED,
            error::invalid_state(EVESTING_CONTRACT_STILL_ACTIVE)
        );

        let vesting_contract = borrow_global_mut<VestingContract>(contract_address);
        verify_admin(admin, vesting_contract);
        let coins = withdraw_stake(vesting_contract, contract_address);
        let amount = coin::value(&coins);
        if (amount == 0) {
            coin::destroy_zero(coins);
            return
        };
        aptos_account::deposit_coins(vesting_contract.withdrawal_address, coins);

        emit(
            AdminWithdraw {
                admin: vesting_contract.admin,
                vesting_contract_address: contract_address,
                amount,
            },
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_proxy.move (L31-41)
```text
    public entry fun set_vesting_contract_operator(owner: &signer, old_operator: address, new_operator: address) {
        let owner_address = signer::address_of(owner);
        let vesting_contracts = &vesting::vesting_contracts(owner_address);
        vesting_contracts.for_each_ref(|vesting_contract| {
            let vesting_contract = *vesting_contract;
            if (vesting::operator(vesting_contract) == old_operator) {
                let current_commission_percentage = vesting::operator_commission_percentage(vesting_contract);
                vesting::update_operator(owner, vesting_contract, new_operator, current_commission_percentage);
            };
        });
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_proxy.move (L58-67)
```text
    public entry fun set_vesting_contract_voter(owner: &signer, operator: address, new_voter: address) {
        let owner_address = signer::address_of(owner);
        let vesting_contracts = &vesting::vesting_contracts(owner_address);
        vesting_contracts.for_each_ref(|vesting_contract| {
            let vesting_contract = *vesting_contract;
            if (vesting::operator(vesting_contract) == operator) {
                vesting::update_voter(owner, vesting_contract, new_voter);
            };
        });
    }
```

**File:** aptos-move/framework/aptos-framework/sources/genesis.move (L282-293)
```text
            let contract_address = vesting::create_vesting_contract(
                admin_signer,
                &employee_group.accounts,
                buy_ins,
                vesting_schedule,
                admin,
                employee_group.validator.validator_config.operator_address,
                employee_group.validator.validator_config.voter_address,
                employee_group.validator.commission_percentage,
                x"",
            );
            let pool_address = vesting::stake_pool_address(contract_address);
```
