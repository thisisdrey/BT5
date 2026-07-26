### Title
Delegation-Pool Delegator Can Bypass Transaction Execution/IO Limits by Withdrawing Qualifying `pending_inactive` Stake in the Same Transaction — (`aptos-move/framework/aptos-framework/sources/transaction_limits.move`)

---

### Summary

`transaction_limits::validate_high_txn_limits` for the `DelegationPoolDelegator` variant counts both `active` and `pending_inactive` delegation-pool stake toward the minimum-stake threshold required to unlock higher execution/IO gas multipliers. Because the prologue check is a point-in-time snapshot taken before the transaction body executes, a delegator whose lockup has already expired can satisfy the threshold using `pending_inactive` stake and then immediately withdraw that same stake inside the transaction body. The high multiplier (up to 100×) is already granted for the transaction, so the stake commitment is never actually enforced.

---

### Finding Description

`transaction_limits.move` exposes `validate_high_txn_limits`, called from `prologue_common` in `transaction_validation.move`, to let a fee-payer claim elevated execution and IO gas limits by proving sufficient stake. For the `DelegationPoolDelegator` path the check is:

```move
// transaction_limits.move lines 306-309
let (active, _, pending_inactive) = delegation_pool::get_stake(
    pool_address, fee_payer
);
validate_enough_stake(active + pending_inactive, multipliers);
``` [1](#0-0) 

`pending_inactive` is stake that has been unlocked via `delegation_pool::unlock` and is waiting for the lockup cycle to expire before it can be withdrawn. Once the lockup expires, `delegation_pool::withdraw` can redeem it immediately.

The prologue runs before the transaction body:

```move
// transaction_validation.move lines 187-192
if (txn_limits_request.is_some()) {
    transaction_limits::validate_high_txn_limits(
        gas_payer_address,
        txn_limits_request.destroy_some(),
    );
};
``` [2](#0-1) 

Because the high-limit grant is irrevocable once the prologue passes, the transaction body is free to call `delegation_pool::withdraw` and drain the very `pending_inactive` stake that satisfied the threshold. There is no post-execution check that the stake still exists.

The `StakePoolOwner` and `DelegatedVoter` variants are not affected because they use the pool's total voting power (`aptos_governance::get_voting_power`), which a single delegator cannot drain in one transaction. Only the `DelegationPoolDelegator` path exposes per-delegator `pending_inactive` stake that is individually withdrawable. [3](#0-2) 

---

### Impact Explanation

An unprivileged delegator can obtain up to 100× the base execution and IO gas limits for a transaction without maintaining any lasting stake commitment. Concretely:

- **Bypassing execution/IO limits**: The attacker submits a transaction that would normally be rejected or throttled under 1× limits (e.g., a very large computation or bulk state write), using the 8× or 100× multiplier obtained via the stake-check bypass.
- **Validator resource exhaustion**: Transactions executing at 100× the base limit consume proportionally more validator CPU and IO. If the base limits are calibrated to prevent DoS, a sustained stream of such transactions could degrade validator throughput or cause availability issues.
- **No lasting cost**: The `pending_inactive` stake is withdrawn in the same transaction, so the attacker recovers their APT immediately. The only cost is the gas fee for the transaction itself.

---

### Likelihood Explanation

The precondition is that the attacker holds `pending_inactive` stake in a delegation pool whose lockup cycle has expired. This is a normal, reachable state for any delegator who has previously called `delegation_pool::unlock` and waited for the lockup to expire. No privileged access, governance action, or special timing is required beyond submitting a single crafted transaction with a `UserTxnLimitsRequest::DelegationPoolDelegator` payload.

---

### Recommendation

The stake check must reflect stake that cannot be removed within the same transaction. Two complementary fixes:

1. **Exclude `pending_inactive` from the threshold**: Count only `active` stake, which cannot be withdrawn in a single transaction (it must first be unlocked, then wait for the lockup to expire across an epoch boundary).

   ```move
   let (active, _, _pending_inactive) = delegation_pool::get_stake(pool_address, fee_payer);
   validate_enough_stake(active, multipliers);
   ```

2. **Alternatively, snapshot and re-verify**: Record the stake amount at prologue and assert it is unchanged (or still above threshold) in the epilogue. This is more complex but preserves the intent of counting `pending_inactive`.

The root cause is identical to the Perennial M-6 finding: a threshold is checked at one moment but the qualifying resource can be immediately removed, making the threshold illusory. The fix in both cases is to require a resource that cannot be atomically removed within the same transaction.

---

### Proof of Concept

```
Setup:
  - Governance configures: execution 8× requires 50 APT, IO 8× requires 100 APT.
  - Delegator D has 100 APT in pending_inactive state in pool P, lockup expired.
  - D's active stake = 0.

Step 1 (prologue):
  D submits a transaction with payload:
    UserTxnLimitsRequest::DelegationPoolDelegator {
        pool_address: P,
        multipliers: RequestedMultipliers::V1 {
            execution_multiplier_percent: 800,  // 8×
            io_multiplier_percent: 800,         // 8×
        }
    }

  validate_high_txn_limits reads:
    (active=0, _, pending_inactive=100 APT) = delegation_pool::get_stake(P, D)
    validate_enough_stake(0 + 100 APT, 8×/8×)  → passes (100 APT ≥ 100 APT IO threshold)

  Transaction is granted 8× execution and 8× IO limits.

Step 2 (transaction body):
  D calls delegation_pool::withdraw(D, P, 100 APT).
  The 100 APT pending_inactive stake is transferred to D's wallet.
  D's stake in pool P is now 0.

Result:
  - D executed a transaction with 8× limits.
  - D's net APT balance is unchanged (stake recovered).
  - The stake requirement was never actually committed.
  - D can repeat this every transaction as long as they re-deposit and re-unlock
    (or use a flash-loan equivalent across transactions).
```

The `delegation_pool::withdraw` path that makes this possible: [4](#0-3)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/transaction_limits.move (L297-311)
```text
            DelegationPoolDelegator { pool_address, multipliers } => {
                assert!(
                    delegation_pool::delegation_pool_exists(pool_address),
                    error::not_found(EDELEGATION_POOL_NOT_FOUND)
                );
                assert!(
                    stake::is_current_epoch_validator(pool_address),
                    error::permission_denied(EPOOL_NOT_IN_VALIDATOR_SET)
                );
                let (active, _, pending_inactive) = delegation_pool::get_stake(
                    pool_address, fee_payer
                );
                validate_enough_stake(active + pending_inactive, multipliers);
            }
        }
```

**File:** aptos-move/framework/aptos-framework/sources/transaction_validation.move (L187-192)
```text
        if (txn_limits_request.is_some()) {
            transaction_limits::validate_high_txn_limits(
                gas_payer_address,
                txn_limits_request.destroy_some(),
            );
        };
```

**File:** aptos-move/framework/aptos-framework/sources/delegation_pool.move (L1613-1623)
```text
    /// Withdraw `amount` of owned inactive stake from the delegation pool at `pool_address`.
    public entry fun withdraw(
        delegator: &signer,
        pool_address: address,
        amount: u64
    ) acquires DelegationPool, GovernanceRecords, BeneficiaryForOperator, NextCommissionPercentage {
        assert!(amount > 0, error::invalid_argument(EWITHDRAW_ZERO_STAKE));
        // synchronize delegation and stake pools before any user operation
        synchronize_delegation_pool(pool_address);
        withdraw_internal(borrow_global_mut<DelegationPool>(pool_address), signer::address_of(delegator), amount);
    }
```
