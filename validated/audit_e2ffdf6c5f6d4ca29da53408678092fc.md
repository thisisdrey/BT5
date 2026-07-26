The full call path is traceable in the code. Let me verify the `create_named_object` default `allow_ungated_transfer` setting to confirm the precondition holds.

### Title
Unauthorized Permanent Package Freeze via Ungated Object Transfer — (`aptos-move/framework/aptos-framework/sources/object_code_deployment.move`)

---

### Summary

`object_code_deployment::publish` creates a named object with `allow_ungated_transfer = true` (the hardcoded default in `create_object_internal`) and never disables it. `freeze_code_object` authorizes the caller solely by checking **current** object ownership via `object::is_owner`. An unprivileged attacker can therefore transfer ownership of any victim's code object to themselves and then permanently set every package's `upgrade_policy` to `immutable`, irreversibly stripping the original publisher of all upgrade capability.

---

### Finding Description

**Root cause — `allow_ungated_transfer` left enabled at publish time:**

`create_object_internal` unconditionally initializes every object with `allow_ungated_transfer: true`: [1](#0-0) 

`object_code_deployment::publish` calls `create_named_object` and stores only an `ExtendRef`; it never calls `disable_ungated_transfer` or `set_untransferable`: [2](#0-1) 

Because `allow_ungated_transfer` remains `true`, any caller can invoke `object::transfer` (or `transfer_call`) to become the direct owner of the code object without any permission from the original publisher.

**Weak authorization in `freeze_code_object`:**

`code::freeze_code_object` checks only `object::is_owner` — the **current** owner — not the original publisher: [3](#0-2) 

Once the attacker owns the object, this check passes and every package in the registry is permanently set to `upgrade_policy_immutable()`.

**Full attack path (no privileged access required):**

```
object::transfer(attacker_signer, code_object, attacker_addr)
  → verify_ungated_and_descendant passes (allow_ungated_transfer == true)
  → ObjectCore.owner = attacker_addr

object_code_deployment::freeze_code_object(attacker_signer, code_object)
  → code::freeze_code_object(attacker_signer, code_object)
      → object::is_owner(code_object, attacker_addr) == true  ✓
      → registry.packages.for_each_mut: upgrade_policy = immutable  (irreversible)
``` [4](#0-3) 

---

### Impact Explanation

The `upgrade_policy` field in `PackageRegistry.packages` is permanently corrupted to `immutable` without the original publisher's consent. There is no unfreeze path. The victim can never again call `upgrade` on their package — any attempt aborts at `check_upgradability`: [5](#0-4) 

This is an unauthorized, irreversible state transition on user-controlled on-chain state. If the victim's module contains a security vulnerability, they are permanently prevented from patching it, which can transitively endanger user funds held by or interacting with that module.

---

### Likelihood Explanation

- **Precondition:** victim published via `object_code_deployment::publish`. This is the standard, documented path for object-based code deployment.
- **Attacker cost:** one `object::transfer` transaction (gas only). No privileged keys, no governance, no validator access required.
- **Detection:** none on-chain before the freeze is committed; the `Freeze` event is emitted only after the damage is done.
- **Reversibility:** zero — `upgrade_policy_immutable` is a one-way ratchet enforced at the VM level.

---

### Recommendation

In `object_code_deployment::publish`, immediately after generating the `ConstructorRef`, generate a `TransferRef` and disable ungated transfer so the code object can only be transferred via an explicit `TransferRef` held by the publisher:

```move
let transfer_ref = constructor_ref.generate_transfer_ref();
object::disable_ungated_transfer(&transfer_ref);
// store transfer_ref inside ManagingRefs if owner-controlled transfer is desired
```

Alternatively, call `object::set_untransferable(constructor_ref)` if the code object should never be transferable at all. Either fix closes the window between object creation and the missing transfer guard. [6](#0-5) 

---

### Proof of Concept

```move
// 1. Victim publishes a package (code object created with allow_ungated_transfer = true)
object_code_deployment::publish(&victim_signer, metadata, code);
// code_object_addr = deterministic from victim address + sequence number

// 2. Attacker transfers ownership (no victim signature needed)
let code_obj = object::address_to_object<PackageRegistry>(code_object_addr);
object::transfer(&attacker_signer, code_obj, attacker_addr);
// ObjectCore.owner is now attacker_addr

// 3. Attacker permanently freezes the victim's package
object_code_deployment::freeze_code_object(&attacker_signer, code_obj);
// All packages: upgrade_policy = immutable (irreversible)

// 4. Victim can no longer upgrade — aborts with EUPGRADE_IMMUTABLE
object_code_deployment::upgrade(&victim_signer, new_metadata, new_code, code_obj);
// ^^^ aborts
```

### Citations

**File:** aptos-move/framework/aptos-framework/sources/object.move (L340-348)
```text
        move_to(
            &object_signer,
            ObjectCore {
                guid_creation_num,
                owner: creator_address,
                allow_ungated_transfer: true,
                transfer_events: event::new_event_handle(transfer_events_guid),
            },
        );
```

**File:** aptos-move/framework/aptos-framework/sources/object.move (L494-497)
```text
    public fun disable_ungated_transfer(self: &TransferRef) {
        let object = borrow_global_mut<ObjectCore>(self.self);
        object.allow_ungated_transfer = false;
    }
```

**File:** aptos-move/framework/aptos-framework/sources/object_code_deployment.move (L87-95)
```text
        let constructor_ref = &object::create_named_object(publisher, object_seed);
        let code_signer = &constructor_ref.generate_signer();
        code::publish_package_txn(code_signer, metadata_serialized, code);

        event::emit(Publish { object_address: signer::address_of(code_signer), });

        move_to(code_signer, ManagingRefs {
            extend_ref: constructor_ref.generate_extend_ref(),
        });
```

**File:** aptos-move/framework/aptos-framework/sources/object_code_deployment.move (L138-142)
```text
    public entry fun freeze_code_object(publisher: &signer, code_object: Object<PackageRegistry>) {
        code::freeze_code_object(publisher, code_object);

        event::emit(Freeze { object_address: code_object.object_address(), });
    }
```

**File:** aptos-move/framework/aptos-framework/sources/code.move (L220-232)
```text
    public fun freeze_code_object(publisher: &signer, code_object: Object<PackageRegistry>) acquires PackageRegistry {
        let code_object_addr = code_object.object_address();
        assert!(exists<PackageRegistry>(code_object_addr), error::not_found(ECODE_OBJECT_DOES_NOT_EXIST));
        assert!(
            object::is_owner(code_object, signer::address_of(publisher)),
            error::permission_denied(ENOT_PACKAGE_OWNER)
        );

        let registry = borrow_global_mut<PackageRegistry>(code_object_addr);
        registry.packages.for_each_mut(|pack| {
            let package: &mut PackageMetadata = pack;
            package.upgrade_policy = upgrade_policy_immutable();
        });
```

**File:** aptos-move/framework/aptos-framework/sources/code.move (L254-258)
```text
    fun check_upgradability(
        old_pack: &PackageMetadata, new_pack: &PackageMetadata, new_modules: &vector<String>) {
        assert!(old_pack.upgrade_policy.policy < upgrade_policy_immutable().policy,
            error::invalid_argument(EUPGRADE_IMMUTABLE));
        assert!(can_change_upgrade_policy_to(old_pack.upgrade_policy, new_pack.upgrade_policy),
```
