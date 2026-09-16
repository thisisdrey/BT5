### Title
Front-running `SetAccountIdContract` permanently denies legitimate users their chosen account ID - ([File: actuator/src/main/java/org/tron/core/actuator/SetAccountIdActuator.java])

### Summary
`SetAccountIdContract` transactions are broadcast to the p2p network/mempool before being packed into a block, exposing the `accountId` value they carry. Because the global `AccountIdIndexStore` enforces a case-insensitive, one-time, irreversible uniqueness constraint on `accountId`, an attacker who observes a pending `SetAccountIdContract` transaction can copy the plaintext `accountId` field into their own transaction (signed with their own key, targeting their own address) and get it mined first. This causes the original (victim) transaction to permanently fail, and the desired identifier can never be reclaimed by the victim — the exact "front-run and cause the legitimate transaction to fail" pattern described in the `permit()` DOS report, but here the state that gets front-run (a unique index entry) is irrevocable rather than just consumed.

### Finding Description
`SetAccountIdActuator.validate()` rejects the operation if the account already has an ID set or if the requested `accountId` already exists in `AccountIdIndexStore`: [1](#0-0) 

`execute()` unconditionally writes the `accountId` onto the caller's own `AccountCapsule` and indexes it in `AccountIdIndexStore`: [2](#0-1) 

`AccountIdIndexStore` is a global, case-insensitive keyed store (`getLowerCaseAccountId`) shared by all accounts on the chain — any account can claim any not-yet-used ID: [3](#0-2) 

Unlike a normal transfer, `SetAccountIdContract` does not require any pre-existing relationship between the sender and the resource being claimed (the `accountId` string) — the parameter itself is the contested resource, and it becomes visible to everyone the moment the victim's transaction enters the mempool/`pushTransaction` pending pool. The unit test `twiceUpdateAccount`/`nameAlreadyUsed` confirms the second call (by anyone) with an already-claimed ID or already-set account permanently fails with "This account id already set" / "This id has existed": [4](#0-3) 

Because there is no cost differentiation (fee is fixed at 0, see `calcFee()`), and the constraint is a first-come-first-served unique index with no expiry or rotation mechanism, once claimed by the attacker, the ID is permanently unavailable to the legitimate owner, mirroring the "permanent activation blocks the legitimate call" pattern from the report.

### Impact Explanation
The victim's `SetAccountIdContract` transaction fails deterministically after being front-run, and the specific human-readable `accountId` they intended to bind to their address becomes permanently unobtainable for that account, since accountId can only be set once and the index entry can never be freed. This is a persistent denial-of-service on a distinct, irrevocable on-chain resource (analogous to ENS-style username squatting via mempool front-running), directly matching the reported bug class of "transaction DOS via front-running of public/mempool-visible operation parameters."

### Likelihood Explanation
Any node/RPC client can observe pending transactions containing `SetAccountIdContract` (e.g., via `TransactionsMsgHandler` broadcast or mempool inspection) and construct/broadcast a competing transaction with an identical `accountId` targeting their own address, requiring no privileged access, no capital beyond ordinary transaction fees, and only standard signature capability of an unprivileged account.

### Recommendation
Avoid deriving contested global uniqueness directly from unauthenticated, mempool-visible plaintext parameters. Consider a commit-reveal scheme for `SetAccountIdContract` (commit a hash of the desired ID + owner address + salt, then reveal after commitment is finalized), or bind the eventual claim to the original transaction's sender via a nonce/priority mechanism so that copying the visible parameters into a new transaction cannot pre-empt the original submitter's claim.

### Proof of Concept
1. Victim account `A` broadcasts a signed `SetAccountIdContract{ownerAddress=A, accountId="alice"}`.
2. Attacker observes this pending transaction in the mempool/p2p layer and extracts `accountId="alice"`.
3. Attacker immediately broadcasts `SetAccountIdContract{ownerAddress=Attacker, accountId="alice"}` with a higher-priority fee/earlier propagation.
4. Attacker's transaction is included first; `AccountIdIndexStore.put` binds `"alice"` to the attacker's address, as shown in `SetAccountIdActuator.execute()`.
5. Victim `A`'s transaction is processed afterward and fails validation with `"This id has existed"` per `SetAccountIdActuator.validate()` — `A` can never claim `"alice"`. [5](#0-4)

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/SetAccountIdActuator.java (L45-53)
```java
    byte[] ownerAddress = setAccountIdContract.getOwnerAddress().toByteArray();
    AccountCapsule account = accountStore.get(ownerAddress);

    account.setAccountId(setAccountIdContract.getAccountId().toByteArray());
    accountStore.put(ownerAddress, account);
    accountIdIndexStore.put(account);
    ret.setStatus(fee, code.SUCESS);

    return true;
```

**File:** actuator/src/main/java/org/tron/core/actuator/SetAccountIdActuator.java (L87-96)
```java
    AccountCapsule account = accountStore.get(ownerAddress);
    if (account == null) {
      throw new ContractValidateException("Account has not existed");
    }
    if (account.getAccountId() != null && !account.getAccountId().isEmpty()) {
      throw new ContractValidateException("This account id already set");
    }
    if (accountIdIndexStore.has(accountId)) {
      throw new ContractValidateException("This id has existed");
    }
```

**File:** chainbase/src/main/java/org/tron/core/store/AccountIdIndexStore.java (L23-32)
```java
  private static byte[] getLowerCaseAccountId(byte[] bsAccountId) {
    return ByteString
        .copyFromUtf8(ByteString.copyFrom(bsAccountId).toStringUtf8().toLowerCase(Locale.ROOT))
        .toByteArray();
  }

  public void put(AccountCapsule accountCapsule) {
    byte[] lowerCaseAccountId = getLowerCaseAccountId(accountCapsule.getAccountId().toByteArray());
    super.put(lowerCaseAccountId, new BytesCapsule(accountCapsule.getAddress().toByteArray()));
  }
```

**File:** framework/src/test/java/org/tron/core/actuator/SetAccountIdActuatorTest.java (L173-217)
```java
  @Test
  public void nameAlreadyUsed() {
    TransactionResultCapsule ret = new TransactionResultCapsule();
    SetAccountIdActuator actuator = new SetAccountIdActuator();
    actuator.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(ACCOUNT_NAME, OWNER_ADDRESS));
    SetAccountIdActuator actuator1 = new SetAccountIdActuator();
    actuator1.setChainBaseManager(dbManager.getChainBaseManager())
        .setAny(getContract(ACCOUNT_NAME, OWNER_ADDRESS_1));
    try {
      actuator.validate();
      actuator.execute(ret);
      Assert.assertEquals(ret.getInstance().getRet(), code.SUCESS);
      AccountCapsule accountCapsule = dbManager.getAccountStore()
          .get(ByteArray.fromHexString(OWNER_ADDRESS));
      Assert.assertEquals(ACCOUNT_NAME, accountCapsule.getAccountId().toStringUtf8());
      Assert.assertTrue(true);
    } catch (ContractValidateException e) {
      logger.info(e.getMessage());
      Assert.assertFalse(e instanceof ContractValidateException);
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }

    AccountCapsule ownerCapsule =
        new AccountCapsule(
            ByteString.copyFrom(ByteArray.fromHexString(OWNER_ADDRESS_1)),
            ByteString.EMPTY,
            AccountType.Normal);
    dbManager.getAccountStore().put(ownerCapsule.getAddress().toByteArray(), ownerCapsule);

    try {
      actuator1.validate();
      actuator1.execute(ret);
      Assert.assertFalse(true);
    } catch (ContractValidateException e) {
      Assert.assertTrue(e instanceof ContractValidateException);
      Assert.assertEquals("This id has existed", e.getMessage());
      AccountCapsule accountCapsule = dbManager.getAccountStore()
          .get(ByteArray.fromHexString(OWNER_ADDRESS));
      Assert.assertEquals(ACCOUNT_NAME, accountCapsule.getAccountId().toStringUtf8());
    } catch (ContractExeException e) {
      Assert.assertFalse(e instanceof ContractExeException);
    }
  }
```
