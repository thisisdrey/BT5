[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** contracts/defuse/src/contract/accounts/account/entry/v1.rs (L32-53)
```rust
impl From<AccountV1> for Account {
    fn from(
        AccountV1 {
            nonces,
            flags,
            public_keys,
            state,
            prefix,
        }: AccountV1,
    ) -> Self {
        Self {
            nonces: MaybeLegacyAccountNonces::with_legacy(
                nonces,
                LookupMap::with_hasher(prefix.as_slice().nest(AccountPrefix::OptimizedNonces)),
            ),
            flags,
            public_keys,
            state,
            prefix,
        }
    }
}
```

**File:** contracts/defuse/src/contract/accounts/state.rs (L7-11)
```rust
#[cfg_attr(feature = "abi", derive(::borsh::BorshSchema))]
#[derive(Debug, BorshSerialize, BorshDeserialize)]
pub struct AccountState {
    pub token_balances: Amounts<IterableMap<TokenId, u128>>,
}
```

**File:** contracts/defuse/src/contract/accounts/account/nonces.rs (L45-67)
```rust
    #[inline]
    pub fn commit(&mut self, nonce: Nonce) -> Result<()> {
        // Check legacy maps for used nonce
        if self
            .legacy
            .as_ref()
            .is_some_and(|legacy| legacy.is_used(nonce))
        {
            return Err(DefuseError::NonceUsed);
        }

        // New nonces can be committed only to the new map
        self.nonces.commit(nonce)
    }

    #[inline]
    pub fn is_used(&self, nonce: Nonce) -> bool {
        self.nonces.is_used(nonce)
            || self
                .legacy
                .as_ref()
                .is_some_and(|legacy| legacy.is_used(nonce))
    }
```

**File:** contracts/defuse/src/contract/accounts/account/entry/mod.rs (L49-53)
```rust
    // When upgrading to a new version, given current version `N`:
    // 1. Copy current `Account` struct definition and name it `AccountVN`
    // 2. Add variant `VN(Cow<'a, PanicOnClone<Lock<AccountVN>>>)` before `Latest`
    // 3. Handle new variant in `match` expessions below
    // 4. Add tests for `VN -> Latest` migration
```

**File:** contracts/defuse/src/contract/accounts/account/entry/mod.rs (L110-134)
```rust
impl BorshDeserializeAs<Lock<Account>> for MaybeVersionedAccountEntry {
    fn deserialize_as<R>(reader: &mut R) -> io::Result<Lock<Account>>
    where
        R: io::Read,
    {
        // There will always be 4 bytes for u32:
        // * either `VERSIONED_MAGIC_PREFIX`,
        // * or u32 for `Account.nonces.prefix`
        let mut buf = [0u8; size_of::<u32>()];
        reader.read_exact(&mut buf)?;
        let prefix = u32::deserialize_reader(&mut buf.as_slice())?;

        if prefix == Self::VERSIONED_MAGIC_PREFIX {
            VersionedAccountEntry::deserialize_reader(reader)
        } else {
            // legacy account
            AccountV0::deserialize_reader(
                // prepend already consumed part of the reader
                &mut buf.chain(reader),
            )
            .map(Into::into)
        }
        .map(Into::into)
    }
}
```

**File:** contracts/defuse/src/contract/accounts/account/entry/tests/legacy.rs (L59-95)
```rust
#[rstest]
fn legacy_upgrade(
    #[from(make_arbitrary)] data: AccountData,
    #[from(make_arbitrary)] random_nonces: Vec<U256>,
) {
    // legacy accounts have no wrappers around them
    let legacy_acc = data.make_legacy_account::<AccountV0>();
    let serialized_legacy = borsh::to_vec(&legacy_acc).expect("unable to serialize legacy Account");

    // we need to drop it, so all collections from near-sdk flush to storage
    drop(legacy_acc);

    deserialize_and_check_legacy_account(&serialized_legacy, &data, &random_nonces);
}

#[rstest]
#[case::v0(PhantomData::<Lock<AccountV1>>)]
#[allow(clippy::used_underscore_binding)]
fn versioned_upgrade<T>(
    #[from(make_arbitrary)] data: AccountData,
    #[from(make_arbitrary)] random_nonces: Vec<U256>,
    #[case] _marker: PhantomData<T>,
) where
    T: LegacyAccountBuilder + BorshSerialize + BorshDeserialize,
    for<'a> VersionedAccountEntry<'a>: From<&'a T>,
{
    // versioned accounts always have wrappers around them and should be serialized with prefix

    let legacy_entry = data.make_legacy_account::<T>();
    let serialized_legacy = to_vec_as::<_, MaybeVersionedAccountEntry>(&legacy_entry)
        .expect("unable to serialize legacy Account");

    // we need to drop it, so all collections from near-sdk flush to storage
    drop(legacy_entry);

    deserialize_and_check_legacy_account(&serialized_legacy, &data, &random_nonces);
}
```

**File:** contracts/defuse/src/contract/accounts/account/entry/tests/legacy.rs (L134-146)
```rust
    fn assert_contained_in(&self, a: &Account) {
        for pk in &self.public_keys {
            assert!(a.has_public_key(&self.account_id, pk));
        }

        for &n in &self.nonces {
            assert!(a.is_nonce_used(n));
        }

        for (token_id, &amount) in &self.token_balances {
            assert_eq!(a.token_balances.amount_for(token_id), amount);
        }
    }
```
