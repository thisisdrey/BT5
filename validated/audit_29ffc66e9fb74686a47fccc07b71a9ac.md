### Title
Push-Distribution Loop in `vesting::distribute` Aborts on Any Frozen Recipient, Permanently Blocking All Shareholders' APT — (File: `aptos-move/framework/aptos-framework/sources/vesting.move`)

---

### Summary

`vesting::distribute` iterates over every shareholder in a single transaction and calls `aptos_account::deposit_coins` for each one. If any one recipient's `CoinStore<AptosCoin>` is frozen, `coin::deposit` aborts, the entire transaction reverts, and **no shareholder receives any APT**. The same structural flaw exists in `staking_contract::distribute_internal`. Because the loop is atomic and there is no per-recipient error isolation, a single bad recipient state permanently wedges the distribution path for the whole contract.

---

### Finding Description

`vesting::distribute` is a `public entry fun` callable by any address: [1](#0-0) 

The function withdraws all unlocked stake into a local `Coin<AptosCoin>` value, then iterates over every shareholder in the grant pool: [2](#0-1) 

For each shareholder it calls:

```move
aptos_account::deposit_coins(recipient_address, share_of_coins);
```

`aptos_account::deposit_coins` ultimately calls `coin::deposit<CoinType>(to, coins)`: [3](#0-2) 

`coin::deposit` aborts unconditionally when the target `CoinStore` is frozen, as formally specified: [4](#0-3) 

Because Move transactions are atomic, an abort at any iteration reverts the entire transaction — including all prior successful deposits and the `withdraw_stake` call. The coins return to the stake pool, but the distribution cannot make forward progress as long as the problematic account state persists.

The identical push-loop pattern exists in `staking_contract::distribute_internal`: [5](#0-4) 

The formal verifier itself acknowledges the loop cannot be verified for abort conditions: [6](#0-5) 

---

### Impact Explanation

All shareholders in the vesting contract are permanently denied their vested APT distributions for as long as any one recipient address is in a state that causes `deposit_coins` to abort. Concretely:

- **Frozen CoinStore**: If any shareholder's (or their beneficiary's) `CoinStore<AptosCoin>` is frozen, every call to `distribute` reverts. Vested APT accumulates in the stake pool but cannot be claimed by anyone.
- **Disabled direct transfers + unregistered coin**: If a beneficiary address has called `set_allow_direct_coin_transfers(false)` and is not registered for the distributed coin type, `deposit_coins` aborts at the `can_receive_direct_coin_transfers` assertion.

The admin's `update_beneficiary` function allows redirecting any shareholder's payout to an arbitrary address. A misconfigured or malicious beneficiary address (e.g., a reserved system address, or one with a frozen store) is sufficient to wedge the entire contract.

The impact is **permanent loss of access to user-controlled APT staking/vesting balances** — a direct match to the allowed impact gate.

---

### Likelihood Explanation

- `distribute` is a `public entry fun` with no access control; any address can invoke it, so the failure is observable and reproducible by anyone.
- A frozen `CoinStore` can result from on-chain governance freezing an account for compliance reasons — a realistic mainnet event.
- The `update_beneficiary` admin path can accidentally or deliberately introduce an unresolvable recipient.
- `distribute_many` compounds the risk by batching multiple contracts into one transaction. [7](#0-6) 

---

### Recommendation

Replace the push-distribution loop with a **pull-over-push** pattern:

1. Record each shareholder's claimable amount in a per-address table during `distribute` (no external calls, no abort risk).
2. Add a separate `claim(contract_address)` entry function that each shareholder calls individually to withdraw their own share.
3. This isolates per-recipient failures: a frozen account blocks only that account's claim, not the entire distribution.

For the dust remainder sent to `withdrawal_address` (line 744), apply the same pull pattern.

The same refactor should be applied to `staking_contract::distribute_internal`.

---

### Proof of Concept

1. Deploy a vesting contract with shareholders `[A, B]` and a non-zero grant.
2. Wait for stake to unlock so `distribute` has coins to distribute.
3. As the Aptos framework (or via governance), call `coin::freeze_coin_store<AptosCoin>(B_address, freeze_cap)` to freeze B's store.
4. Call `vesting::distribute(contract_address)` from any address.
5. Observe: the transaction aborts with `ESTORE_IS_FROZEN` when attempting to deposit to B.
6. Shareholder A's APT is also not delivered (transaction fully reverted).
7. Repeat step 4 indefinitely — every call aborts. A can never claim their vested APT while B's store remains frozen.

The same sequence applies to `staking_contract::distribute` with the staker/operator distribution pool.

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L719-740)
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
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L758-768)
```text
    /// Call `distribute` for many vesting contracts.
    public entry fun distribute_many(contract_addresses: vector<address>) acquires VestingContract {
        let len = contract_addresses.length();

        assert!(len != 0, error::invalid_argument(EVEC_EMPTY_FOR_MANY_FUNCTION));

        contract_addresses.for_each_ref(|contract_address| {
            let contract_address = *contract_address;
            distribute(contract_address);
        });
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

**File:** aptos-move/framework/aptos-framework/sources/vesting.spec.move (L307-314)
```text
    spec distribute(contract_address: address) {
        // TODO: Can't handle abort in loop.
        pragma verify = false;
        include ActiveVestingContractAbortsIf;

        let vesting_contract = global<VestingContract>(contract_address);
        include WithdrawStakeAbortsIf { vesting_contract };
    }
```
