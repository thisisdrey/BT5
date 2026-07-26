### Title
Multisig Transaction Simultaneously Executable and Rejectable Allows Griefing of Approved Transactions — (`aptos-move/framework/aptos-framework/sources/multisig_account.move`)

---

### Summary

In `aptos_framework::multisig_account`, `can_be_executed` and `can_be_rejected` both use `>= num_signatures_required` with no mutual-exclusivity guard. When a multisig has enough owners that `num_approvals >= k` and `num_rejections >= k` simultaneously, any owner can call `execute_rejected_transaction` to permanently discard a transaction that already has sufficient approvals to execute — a direct analog to the raffle's `_checkShouldCancel` / `_checkShouldDraw` overlap at `supply == minTicketsThreshold`.

---

### Finding Description

`can_be_executed` and `can_be_rejected` are defined as:

```move
// can_be_executed: true when approvals >= k
public fun can_be_executed(multisig_account: address, sequence_number: u64): bool {
    ...
    num_approvals >= num_signatures_required(multisig_account) && can_execute_with_timelock(...)
}

// can_be_rejected: true when rejections >= k  (no check that approvals < k)
public fun can_be_rejected(multisig_account: address, sequence_number: u64): bool {
    ...
    num_rejections >= num_signatures_required(multisig_account)
}
``` [1](#0-0) 

Neither function checks whether the other condition is also satisfied. When both are true, `execute_rejected_transaction` — a public entry function callable by any owner — removes the transaction and asserts only that `num_rejections >= num_signatures_required`:

```move
let (_, num_rejections) = remove_executed_transaction(multisig_account_resource);
assert!(
    num_rejections >= multisig_account_resource.num_signatures_required,
    error::invalid_state(ENOT_ENOUGH_REJECTIONS),
);
``` [2](#0-1) 

There is no guard that prevents rejection when `num_approvals >= num_signatures_required` is already true.

Additionally, with `multisig_v2_enhancement_feature_enabled()`, `execute_rejected_transaction` implicitly casts a rejection vote for the caller before checking the count:

```move
if (!has_voted_for_rejection(multisig_account, sequence_number, owner_addr)) {
    reject_transaction(owner, multisig_account, sequence_number);
}
``` [3](#0-2) 

This means a previously neutral owner can add the k-th rejection vote and immediately execute the rejection in a single transaction, even if k approvals already exist.

---

### Impact Explanation

**Concrete scenario — 4-owner multisig, `num_signatures_required = 2`:**

1. Owner 1 creates a transaction (auto-approves) → `num_approvals = 1`
2. Owner 2 approves → `num_approvals = 2 ≥ 2`; `can_be_executed = true`
3. Owner 3 rejects → `num_rejections = 1`
4. Owner 4 rejects → `num_rejections = 2 ≥ 2`; `can_be_rejected = true`
5. Owner 3 calls `execute_rejected_transaction` before the VM executes the multisig transaction
6. The transaction is permanently discarded even though it had the required k approvals

If the transaction was a fund transfer or a critical governance action, the funds remain locked in the multisig account and the intended operation never executes. The k approving owners have no recourse — the transaction is gone and a new one must be created, which can be griefed again.

**With `multisig_v2_enhancement_feature_enabled()` — 5-owner multisig, `num_signatures_required = 3`:**

1. Owners 1, 2, 3 approve → `num_approvals = 3 ≥ 3`; `can_be_executed = true`
2. Owners 4, 5 reject → `num_rejections = 2`
3. A neutral owner (or owner 4/5 acting again via a different path) is not needed here — but if `k-1` rejections exist and one neutral owner calls `execute_rejected_transaction`, their implicit vote brings `num_rejections` to `k`, and the transaction is rejected despite having full approval quorum.

---

### Likelihood Explanation

The condition `num_approvals >= k AND num_rejections >= k` requires `n >= 2k` total owners. This is impossible for the common 2-of-3 configuration but is reachable for:

- **1-of-2**: one owner approves, one rejects — both conditions met immediately
- **2-of-4**: two approve, two reject
- **3-of-6** and larger configurations

These configurations are realistic for DAOs, treasury multisigs, and protocol governance accounts on Aptos mainnet. The attack requires no special privilege — any owner can call `execute_rejected_transaction`.

---

### Recommendation

Add a mutual-exclusivity guard in `can_be_rejected` (or directly in `execute_rejected_transaction`) that prevents rejection when the transaction already has enough approvals to execute:

```move
public fun can_be_rejected(multisig_account: address, sequence_number: u64): bool {
    assert_valid_sequence_number(multisig_account, sequence_number);
    let (num_approvals, num_rejections) = num_approvals_and_rejections(multisig_account, sequence_number);
    sequence_number == last_resolved_sequence_number(multisig_account) + 1 &&
        num_rejections >= num_signatures_required(multisig_account) &&
        num_approvals < num_signatures_required(multisig_account)  // ← add this guard
}
```

This mirrors the raffle fix: change `supply > minTicketsThreshold` to `supply >= minTicketsThreshold` in the cancel check, making the two states mutually exclusive.

---

### Proof of Concept

```move
#[test(owner_1 = @0x123, owner_2 = @0x124, owner_3 = @0x125, owner_4 = @0x126)]
fun test_reject_griefs_approved_transaction(
    owner_1: &signer, owner_2: &signer, owner_3: &signer, owner_4: &signer
) {
    setup();
    create_account(address_of(owner_1));
    // 4-owner multisig, 2-of-4
    create_with_owners(owner_1,
        vector[address_of(owner_2), address_of(owner_3), address_of(owner_4)],
        2, vector[], vector[]);
    let multisig_account = get_next_multisig_account_address(address_of(owner_1));

    // Owner 1 creates (auto-approves), owner 2 approves → 2 approvals, executable
    create_transaction(owner_1, multisig_account, PAYLOAD);
    approve_transaction(owner_2, multisig_account, 1);
    assert!(can_be_executed(multisig_account, 1), 0);  // passes

    // Owner 3 and 4 reject → 2 rejections, rejectable
    reject_transaction(owner_3, multisig_account, 1);
    reject_transaction(owner_4, multisig_account, 1);
    assert!(can_be_rejected(multisig_account, 1), 1);  // also passes — BUG

    // Griefer calls execute_rejected_transaction before VM executes
    execute_rejected_transaction(owner_3, multisig_account);

    // Transaction is gone despite having sufficient approvals
    assert!(get_pending_transactions(multisig_account) == vector[], 2);
}
``` [4](#0-3) [5](#0-4)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/multisig_account.move (L473-524)
```text
    public fun can_be_executed(multisig_account: address, sequence_number: u64): bool {
        assert_valid_sequence_number(multisig_account, sequence_number);
        let (num_approvals, _) = num_approvals_and_rejections(multisig_account, sequence_number);

        sequence_number == last_resolved_sequence_number(multisig_account) + 1 &&
            num_approvals >= num_signatures_required(multisig_account) && can_execute_with_timelock(multisig_account, sequence_number, num_approvals)
    }

    #[view]
    /// Return true if the owner can execute the transaction with given transaction id now.
    public fun can_execute(owner: address, multisig_account: address, sequence_number: u64): bool {
        assert_valid_sequence_number(multisig_account, sequence_number);
        let (num_approvals, _) = num_approvals_and_rejections(multisig_account, sequence_number);
        if (!has_voted_for_approval(multisig_account, sequence_number, owner)) {
            num_approvals += 1;
        };

        is_owner(owner, multisig_account) &&
            sequence_number == last_resolved_sequence_number(multisig_account) + 1 &&
            num_approvals >= num_signatures_required(multisig_account) && can_execute_with_timelock(multisig_account, sequence_number, num_approvals)
    }

    /// Return true if the transaction with given transaction id can be executed immediately, or it has to wait
    /// for the timelock to expire.
    inline fun can_execute_with_timelock(multisig_account: address, sequence_number: u64, num_approvals: u64): bool {
        if (exists<MultisigAccountTimeLock>(multisig_account)) {
            let multisig_account_resource = &MultisigAccountTimeLock[multisig_account];
            let timelock = multisig_account_resource.timelock_period;
            let override_threshold = multisig_account_resource.override_threshold;

            // Get the pending transaction to check if the timelock has expired
            // Assume that the transaction has already been checked to exist and is valid
            let pending_transaction = get_transaction(multisig_account, sequence_number);

            // Use subtraction to avoid overflow (now_seconds() >= creation_time_secs is always true)
            let elapsed = now_seconds() - pending_transaction.creation_time_secs;

            // If the number of approvals meets the override threshold, or the timelock has expired, allow execution
            (override_threshold.is_some() && &num_approvals >= override_threshold.borrow()) || elapsed >= timelock
        } else {
            true
        }
    }

    #[view]
    /// Return true if the transaction with given transaction id can be officially rejected.
    public fun can_be_rejected(multisig_account: address, sequence_number: u64): bool {
        assert_valid_sequence_number(multisig_account, sequence_number);
        let (_, num_rejections) = num_approvals_and_rejections(multisig_account, sequence_number);
        sequence_number == last_resolved_sequence_number(multisig_account) + 1 &&
            num_rejections >= num_signatures_required(multisig_account)
    }
```

**File:** aptos-move/framework/aptos-framework/sources/multisig_account.move (L1273-1305)
```text
    /// Remove the next transaction if it has sufficient owner rejections.
    public entry fun execute_rejected_transaction(
        owner: &signer,
        multisig_account: address,
    ) {
        assert_multisig_account_exists(multisig_account);
        assert_is_owner(owner, multisig_account);

        let sequence_number = last_resolved_sequence_number(multisig_account) + 1;
        let owner_addr = address_of(owner);
        if (features::multisig_v2_enhancement_feature_enabled()) {
            // Implicitly vote for rejection if the owner has not voted for rejection yet.
            if (!has_voted_for_rejection(multisig_account, sequence_number, owner_addr)) {
                reject_transaction(owner, multisig_account, sequence_number);
            }
        };

        let multisig_account_resource = borrow_global_mut<MultisigAccount>(multisig_account);
        let (_, num_rejections) = remove_executed_transaction(multisig_account_resource);
        assert!(
            num_rejections >= multisig_account_resource.num_signatures_required,
            error::invalid_state(ENOT_ENOUGH_REJECTIONS),
        );

        emit(
            ExecuteRejectedTransaction {
                multisig_account,
                sequence_number,
                num_rejections,
                executor: address_of(owner),
            }
        );
    }
```
