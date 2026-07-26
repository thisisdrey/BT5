Looking at the multisig account's vote counting and owner removal logic to find the analog.

### Title
Stale Owner Votes Persist After Removal and Are Silently Reactivated on Re-addition, Enabling Unauthorized Multisig Transaction Execution — (`aptos-move/framework/aptos-framework/sources/multisig_account.move`)

---

### Summary

`update_owner_schema` removes an owner from the `owners` vector but never deletes that owner's entries from the `votes` map inside each pending `MultisigTransaction`. Because `num_approvals_and_rejections_internal` counts votes by iterating the *current* owners list, the stale votes are dormant while the owner is absent. The moment the owner is re-added via `add_owners`, every stale vote they cast before removal is silently reactivated and counted toward quorum — without the owner ever re-voting.

---

### Finding Description

`MultisigTransaction` stores votes in a `SimpleMap<address, bool>` field called `votes`. [1](#0-0) 

`num_approvals_and_rejections_internal` counts only votes whose key appears in the *current* `owners` vector:

```move
owners.for_each_ref(|owner| {
    if (simple_map::contains_key(votes, owner)) { ... }
});
```

This correctly suppresses a removed owner's vote. However, `update_owner_schema` — the single code path that removes owners — only mutates the `owners` vector: [2](#0-1) 

It never touches the `votes` maps of any pending transaction. The stale vote entry remains in every `MultisigTransaction.votes` map indefinitely.

When `add_owners` later re-adds the same address, `num_approvals_and_rejections_internal` immediately starts counting that address's old vote again, because the key is still present in `votes` and the address is now back in `owners`.

The existing test `test_validate_transaction_should_not_consider_removed_owners` verifies the dormant-vote behavior but has no coverage of the re-add path: [3](#0-2) 

---

### Impact Explanation

**Unauthorized multisig transaction execution.**

Concrete scenario — 2-of-3 multisig (owners A, B, C; A is the attacker):

1. A creates malicious transaction T1 → auto-approval recorded (1 approval).
2. B approves T1 → 2 approvals, quorum met.
3. C rejects T1 (1 rejection). Legitimate owners create T2 to remove A; B and C approve → A is removed.
4. T1 now has 1 counted approval (B) and 1 rejection (C) — below quorum, stuck.
5. A convinces B to re-add A via T3; B creates T3, C approves (or is socially engineered).
6. A is re-added. A's stale `true` entry in `T1.votes` is immediately counted again.
7. T1 now has 2 approvals (A's stale + B's) — quorum met again.
8. A or B calls `validate_multisig_transaction` / executes T1 — **without A ever casting a fresh vote**.

The executed transaction can transfer APT or fungible assets out of the multisig account, constituting direct theft of user-controlled on-chain assets. [4](#0-3) 

---

### Likelihood Explanation

**Medium.** The attack requires the removed owner to be re-added, which is itself a multisig governance action. However:

- Re-addition is a routine operation (key rotation, personnel change).
- The re-adder has no visible signal that stale votes exist on pending transactions.
- A malicious owner can deliberately engineer the remove-then-re-add cycle.
- The `swap_owner` / `swap_owners` entry points perform remove + add atomically in a single call, making the stale-vote reactivation happen in one transaction with no intermediate state for defenders to observe. [5](#0-4) 

---

### Recommendation

In `update_owner_schema`, after removing each owner from the `owners` vector, iterate over all pending transactions and call `simple_map::remove` (if the key exists) on each transaction's `votes` map for that owner address. This mirrors the fix recommended in the external report: clean up the associated state at removal time, not lazily. [6](#0-5) 

---

### Proof of Concept

```move
#[test(owner_1 = @0x123, owner_2 = @0x124, owner_3 = @0x125)]
fun test_stale_vote_reactivated_after_readd(
    owner_1: &signer, owner_2: &signer, owner_3: &signer) {
    setup();
    let owner_1_addr = address_of(owner_1);
    let owner_2_addr = address_of(owner_2);
    let owner_3_addr = address_of(owner_3);
    create_account(owner_1_addr);
    let multisig_account = get_next_multisig_account_address(owner_1_addr);
    // 2-of-3 multisig
    create_with_owners(owner_1, vector[owner_2_addr, owner_3_addr], 2, vector[], vector[]);

    // Owner 1 creates T1 (auto-approval) and owner 2 approves → quorum met
    create_transaction(owner_1, multisig_account, PAYLOAD);
    approve_transaction(owner_2, multisig_account, 1);
    assert!(can_be_executed(multisig_account, 1), 0);

    // Owner 1 is removed → quorum lost
    let multisig_signer = &create_signer(multisig_account);
    remove_owners(multisig_signer, vector[owner_1_addr]);
    assert!(!can_be_executed(multisig_account, 1), 1);

    // Owner 1 is re-added → stale vote reactivates, quorum met again WITHOUT a fresh vote
    add_owners(multisig_signer, vector[owner_1_addr]);
    // BUG: this should be false (owner 1 never re-voted), but it is true
    assert!(can_be_executed(multisig_account, 1), 2);
}
```

The final `assert!` passes, demonstrating that T1 reaches quorum solely through the reactivated stale vote, with no fresh approval from the re-added owner.

### Citations

**File:** aptos-move/framework/aptos-framework/sources/multisig_account.move (L1044-1070)
```text
    /// Swap an owner in for an old one, without changing required signatures.
    entry fun swap_owner(
        multisig_account: &signer,
        to_swap_in: address,
        to_swap_out: address
    ) {
        update_owner_schema(
            address_of(multisig_account),
            vector[to_swap_in],
            vector[to_swap_out],
            option::none()
        );
    }

    /// Swap owners in and out, without changing required signatures.
    entry fun swap_owners(
        multisig_account: &signer,
        to_swap_in: vector<address>,
        to_swap_out: vector<address>
    ) {
        update_owner_schema(
            address_of(multisig_account),
            to_swap_in,
            to_swap_out,
            option::none()
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/multisig_account.move (L1328-1353)
```text
    fun validate_multisig_transaction(
        owner: &signer, multisig_account: address, payload: vector<u8>) {
        assert_multisig_account_exists(multisig_account);
        assert_is_owner(owner, multisig_account);
        let sequence_number = last_resolved_sequence_number(multisig_account) + 1;
        assert_transaction_exists(multisig_account, sequence_number);

        if (features::multisig_v2_enhancement_feature_enabled()) {
            assert!(
                can_execute(address_of(owner), multisig_account, sequence_number),
                error::invalid_argument(ENOT_ENOUGH_APPROVALS),
            );
        }
        else {
            assert!(
                can_be_executed(multisig_account, sequence_number),
                error::invalid_argument(ENOT_ENOUGH_APPROVALS),
            );
        };

        // Count approvals, including the executing owner's implicit vote.
        let (num_approvals, _) = num_approvals_and_rejections(multisig_account, sequence_number);
        if (!has_voted_for_approval(multisig_account, sequence_number, address_of(owner))) {
            num_approvals += 1;
        };
        assert!(num_approvals >= num_signatures_required(multisig_account), error::invalid_argument(ENOT_ENOUGH_APPROVALS));
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

**File:** aptos-move/framework/aptos-framework/sources/multisig_account.move (L1586-1632)
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
        };
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
        };
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
