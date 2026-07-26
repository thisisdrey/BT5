Let me look at the `update_owner_schema` function to understand how owner removal is handled:

Now I have a complete picture. Let me confirm the exact behavior of `num_approvals_and_rejections_internal` and `update_owner_schema` to finalize the analysis.

### Title
Stale Votes Persist in Pending Transactions After Owner Removal and Re-addition in `multisig_account` - (File: `aptos-move/framework/aptos-framework/sources/multisig_account.move`)

---

### Summary

When an owner is removed from a multisig account, their votes on all pending transactions remain in the `transaction.votes` `SimpleMap`. The vote-counting function `num_approvals_and_rejections_internal` filters by the current `owners` list, so the stale votes are dormant while the owner is absent. However, if the same owner address is later re-added via `add_owners` or `swap_owner`, those stale votes are immediately re-counted without the owner explicitly re-voting in their new capacity. This is the direct Aptos analog of the "stale policy state survives removal" bug class.

---

### Finding Description

**Root cause — `update_owner_schema` does not clean pending-transaction votes on removal.**

`update_owner_schema` is the single code path used by `remove_owner`, `remove_owners`, `swap_owner`, and `swap_owners`. It removes the owner address from the `owners` vector but performs no cleanup of the `transactions` table:

```move
// update_owner_schema (lines 1612-1631)
if (owners_to_remove.length() > 0) {
    let owners_ref_mut = &mut multisig_account_ref_mut.owners;
    let owners_removed = vector[];
    owners_to_remove.for_each_ref(|owner_to_remove_ref| {
        let (found, index) =
            vector::index_of(owners_ref_mut, owner_to_remove_ref);
        if (found) {
            vector::push_back(
                &mut owners_removed,
                vector::swap_remove(owners_ref_mut, index)
            );
        }
    });
    // ... emit event
};
// No iteration over multisig_account_ref_mut.transactions to remove stale votes
```

Every `MultisigTransaction` stores votes as a `SimpleMap<address, bool>`:

```move
struct MultisigTransaction has copy, drop, store {
    ...
    votes: SimpleMap<address, bool>,
    ...
}
```

**Vote counting only filters by the current `owners` list at query time:**

```move
// num_approvals_and_rejections_internal (lines 1532-1548)
inline fun num_approvals_and_rejections_internal(
    owners: &vector<address>, transaction: &MultisigTransaction
): (u64, u64) {
    let num_approvals = 0;
    let num_rejections = 0;
    let votes = &transaction.votes;
    owners.for_each_ref(|owner| {
        if (simple_map::contains_key(votes, owner)) {
            if (*simple_map::borrow(votes, owner)) {
                num_approvals += 1;
            } else {
                num_rejections += 1;
            };
        }
    });
    (num_approvals, num_rejections)
}
```

This means:
- While the owner is absent, their vote entry sits silently in `transaction.votes`.
- The moment the same address is re-added to `owners`, the old vote is counted again — with no explicit re-vote required.

The existing test `test_validate_transaction_should_not_consider_removed_owners` (line 2269) confirms the dormant-vote behavior but does **not** test the re-addition path, leaving the stale-vote re-activation undetected.

---

### Impact Explanation

**Concrete attack path (2-of-3 multisig):**

1. Owners A (attacker), B, C form a 2-of-3 multisig.
2. A creates malicious transaction T (e.g., `coin::transfer` all funds to A's address).
3. A votes to approve T → 1/3 approvals.
4. B votes to approve T → 2/3 approvals; T is now executable.
5. C proposes removing B to block T; A and C approve → B is removed; T drops to 1/3 approvals and cannot execute.
6. A proposes re-adding B (perhaps framed as "restoring a trusted owner"). A and C approve → B is re-added.
7. B's old `true` vote is still in `transaction.votes`. `num_approvals_and_rejections_internal` now iterates the updated `owners` list (which includes B again) and counts B's stale vote → T is back at 2/3 approvals.
8. A calls `validate_multisig_transaction` / executes T. Funds are drained.

The re-added owner B never explicitly re-voted in their new tenure; their pre-removal vote silently re-activates.

**Broader impact:** Any pending transaction that was previously blocked by removing a voter can be unblocked by re-adding that voter, without their knowledge or consent. This breaks the invariant that every counted vote must have been cast while the voter was a current owner.

---

### Likelihood Explanation

Likelihood is **low-to-medium**:
- Requires the multisig to execute two additional governance transactions (remove + re-add), each needing k-of-n approval.
- In a 2-of-3 setup the attacker controls one vote; they only need one other owner to approve each governance step, which is achievable through social engineering or if the other owner is unaware of the stale-vote side-effect.
- The behavior is non-obvious: the existing audit documentation (spec.move lines 91-166) does not mention the re-addition scenario, and no test covers it.
- On mainnet, multisig accounts hold real APT and fungible assets, making this a realistic target.

---

### Recommendation

In `update_owner_schema`, after removing an owner from the `owners` vector, iterate over all pending transactions and remove the departing owner's entry from each `transaction.votes` map:

```move
// After removing owner from owners vector:
let seq = multisig_account_ref_mut.last_executed_sequence_number + 1;
let next = multisig_account_ref_mut.next_sequence_number;
while (seq < next) {
    if (multisig_account_ref_mut.transactions.contains(seq)) {
        let tx = multisig_account_ref_mut.transactions.borrow_mut(seq);
        owners_to_remove.for_each_ref(|addr| {
            if (tx.votes.contains_key(addr)) {
                tx.votes.remove(addr);
            }
        });
    };
    seq = seq + 1;
};
```

This ensures that a re-added owner starts with a clean slate on all pending transactions, matching the invariant that only votes cast during a current ownership tenure are counted.

---

### Proof of Concept

Move pseudocode (unit-test style, extending existing test infrastructure):

```move
#[test(owner_1 = @0x123, owner_2 = @0x124, owner_3 = @0x125)]
fun test_stale_vote_after_remove_and_readd(
    owner_1: &signer, owner_2: &signer, owner_3: &signer) {
    setup();
    let o1 = address_of(owner_1);
    let o2 = address_of(owner_2);
    let o3 = address_of(owner_3);
    create_account(o1);
    let ms = get_next_multisig_account_address(o1);
    // 2-of-3 multisig
    create_with_owners(owner_1, vector[o2, o3], 2, vector[], vector[]);

    // Step 1: create malicious transaction T
    create_transaction(owner_1, ms, PAYLOAD);
    // Step 2: owner_2 approves → 2/3, executable
    approve_transaction(owner_2, ms, 1);
    assert!(can_be_executed(ms, 1), 0);

    let ms_signer = &create_signer(ms);
    // Step 3: remove owner_2 → T drops to 1/3, blocked
    remove_owners(ms_signer, vector[o2]);
    assert!(!can_be_executed(ms, 1), 1);

    // Step 4: re-add owner_2 — stale vote re-activates
    add_owners(ms_signer, vector[o2]);
    // BUG: T is executable again without owner_2 explicitly re-voting
    assert!(can_be_executed(ms, 1), 2); // passes — demonstrates the bug
}
```

**Key references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/multisig_account.move (L126-134)
```text
    struct MultisigAccount has key {
        // The list of all owner addresses.
        owners: vector<address>,
        // The number of signatures required to pass a transaction (k in k-of-n).
        num_signatures_required: u64,
        // Map from transaction id (incrementing id) to transactions to execute for this multisig account.
        // Already executed transactions are deleted to save on storage but can always be accessed via events.
        transactions: Table<u64, MultisigTransaction>,
        // The sequence number assigned to the last executed or rejected transaction. Used to enforce in-order
```

**File:** aptos-move/framework/aptos-framework/sources/multisig_account.move (L1532-1548)
```text
    inline fun num_approvals_and_rejections_internal(owners: &vector<address>, transaction: &MultisigTransaction): (u64, u64) {
        let num_approvals = 0;
        let num_rejections = 0;

        let votes = &transaction.votes;
        owners.for_each_ref(|owner| {
            if (simple_map::contains_key(votes, owner)) {
                if (*simple_map::borrow(votes, owner)) {
                    num_approvals += 1;
                } else {
                    num_rejections += 1;
                };
            }
        });

        (num_approvals, num_rejections)
    }
```

**File:** aptos-move/framework/aptos-framework/sources/multisig_account.move (L1586-1610)
```text
    /// Add new owners, remove owners to remove, update signatures required.
    fun update_owner_schema(
        multisig_address: address,
        new_owners: vector<address>,
        owners_to_remove: vector<address>,
        optional_new_num_signatures_required: Option<u64>,
    ) {
        assert_multisig_account_exists(multisig_address);
        let multisig_account_ref_mut =
            borrow_global_mut<MultisigAccount>(multisig_address);
        // Verify no overlap between new owners and owners to remove.
        new_owners.for_each_ref(|new_owner_ref| {
            assert!(
                !vector::contains(&owners_to_remove, new_owner_ref),
                error::invalid_argument(EOWNERS_TO_REMOVE_NEW_OWNERS_OVERLAP)
            )
        });
        // If new owners provided, try to add them and emit an event.
        if (new_owners.length() > 0) {
            multisig_account_ref_mut.owners.append(new_owners);
            validate_owners(
                &multisig_account_ref_mut.owners,
                multisig_address
            );
            emit(AddOwners { multisig_account: multisig_address, owners_added: new_owners });
```

**File:** aptos-move/framework/aptos-framework/sources/multisig_account.move (L1612-1631)
```text
        // If owners to remove provided, try to remove them.
        if (owners_to_remove.length() > 0) {
            let owners_ref_mut = &mut multisig_account_ref_mut.owners;
            let owners_removed = vector[];
            owners_to_remove.for_each_ref(|owner_to_remove_ref| {
                let (found, index) =
                    vector::index_of(owners_ref_mut, owner_to_remove_ref);
                if (found) {
                    vector::push_back(
                        &mut owners_removed,
                        vector::swap_remove(owners_ref_mut, index)
                    );
                }
            });
            // Only emit event if owner(s) actually removed.
            if (owners_removed.length() > 0) {
                emit(
                    RemoveOwners { multisig_account: multisig_address, owners_removed }
                );
            }
```

**File:** aptos-move/framework/aptos-framework/sources/multisig_account.move (L2268-2289)
```text
    #[test(owner_1 = @0x123, owner_2 = @0x124, owner_3 = @0x125)]
    fun test_validate_transaction_should_not_consider_removed_owners(
        owner_1: &signer, owner_2: &signer, owner_3: & signer) {
        setup();
        let owner_1_addr = address_of(owner_1);
        let owner_2_addr = address_of(owner_2);
        let owner_3_addr = address_of(owner_3);
        create_account(owner_1_addr);
        let multisig_account = get_next_multisig_account_address(owner_1_addr);
        create_with_owners(owner_1, vector[owner_2_addr, owner_3_addr], 2, vector[], vector[]);

        // Owner 1 and 2 approved but then owner 1 got removed.
        create_transaction(owner_1, multisig_account, PAYLOAD);
        approve_transaction(owner_2, multisig_account, 1);
        // Before owner 1 is removed, the transaction technically has sufficient approvals.
        assert!(can_be_executed(multisig_account, 1), 0);
        let multisig_signer = &create_signer(multisig_account);
        remove_owners(multisig_signer, vector[owner_1_addr]);
        // Now that owner 1 is removed, their approval should be invalidated and the transaction no longer
        // has enough approvals to be executed.
        assert!(!can_be_executed(multisig_account, 1), 1);
    }
```
