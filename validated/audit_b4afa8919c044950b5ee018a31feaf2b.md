### Title
DoS: Attacker can front-run `SetAccountIdContract` to permanently squat a victim's chosen `account_id` - (File: `actuator/src/main/java/org/tron/core/actuator/SetAccountIdActuator.java`)

### Summary
`SetAccountIdActuator` lets any account permanently bind a globally-unique `account_id` to its address, exactly once, with a uniqueness check keyed only on the `account_id` value itself (case-insensitive). Because the check-then-write is a plain "does this id already exist" validation with no `msg.sender`/owner binding baked into the uniqueness key, an attacker who observes a pending `SetAccountIdContract` transaction in the mempool can submit their own transaction with the identical `account_id` (bound to their own address) and a higher-priority fee, causing the original transaction to fail permanently — the victim can never claim that `account_id` again, since ids can not be released or reassigned. This mirrors the reported `SplitFactory.createSplit()` front-running bug, where a caller-independent, one-time-use key (`merkleRoot`, here `account_id`) is used as the sole gate for a permanent on-chain claim.

### Finding Description
`SetAccountIdActuator.validate()` only checks that the account has not already set an id and that the requested `account_id` is not already present in `AccountIdIndexStore`: [1](#0-0) 

Crucially, this validation is not scoped to `ownerAddress` in any way — any account can submit a `SetAccountIdContract` with any not-yet-taken `account_id`. `execute()` then commits the id irreversibly: [2](#0-1) 

The underlying `AccountIdIndexStore` performs a case-insensitive uniqueness check with no owner component in the key at all — the key is purely a normalized form of the `account_id` string: [3](#0-2) 

Because the account can set its id only once (enforced by `"This account id already set"`) and an id can never be reassigned or freed once claimed by another address (enforced by `"This id has existed"`), the analogous risk from the report applies directly: this is a first-come-first-served claim over a value the legitimate owner picked and broadcast, with no per-sender salting. The existing test suite even documents this exact race outcome (`nameAlreadyUsed` test), showing that once an id is claimed by any address, all subsequent claimants — including the intended, legitimate submitter who was simply out-raced — are permanently rejected: [4](#0-3) 

This is the same root cause pattern as the external report: a `salt`/unique key (`merkleRoot` there, `account_id` here) used as the sole gate to a one-time, irreversible on-chain claim, with no binding to the submitter's identity, allowing mempool front-running to permanently deny the intended claimant that value.

The `account_id` is a first-class, externally queryable identifier: nodes expose `getAccountById` over gRPC/HTTP so that third parties (exchanges, wallets, integrators) can resolve an `account_id` to its bound TRON address (`AccountStore`/`AccountIdIndexStore`, surfaced via `Wallet`, `GetAccountByIdServlet`, and `RpcApiService`). If an attacker races and steals a target’s intended `account_id` before the target's transaction lands, any downstream system that already advertises or expects that id to resolve to the victim's address will instead resolve it to the attacker's address.

### Impact Explanation
Because `account_id` is permanent, globally unique, and externally resolvable via `getAccountById`, a successful front-run permanently denies the legitimate account owner the ability to ever use that identifier, and — depending on how third-party systems (exchanges, custodial wallets, integrators) rely on `account_id` resolution to route deposits or identify counterparties — an attacker who wins the race could have funds or communications misdirected to the attacker's account instead of the victim's, since `account_id → address` now permanently maps to the attacker. There is no cost barrier (`calcFee()` returns `0`) and no way to recover or reassign the identifier, making this a low-cost, high-persistence griefing/hijack vector reachable by any unprivileged transaction broadcaster.

### Likelihood Explanation
`SetAccountIdContract` transactions are unprivileged, broadcast to the public mempool like any other transaction, and their `account_id` field is plaintext and immediately visible before confirmation. An attacker monitoring the mempool needs only to copy the `account_id` field into their own `SetAccountIdContract`, sign it with their own key, and submit it with a competitive fee/priority — the actuator performs no owner-binding check that would prevent this. This requires no special privileges (validator/witness/committee), matching the "unprivileged transaction broadcaster" reachability the report emphasizes.

### Recommendation
Bind the uniqueness key to the submitting account, or require a two-phase commit (e.g., commit a hash of `account_id + ownerAddress` first, then reveal), so that a front-runner cannot claim an identifier intended for a different address without also controlling that address. At minimum, consider allowing an id to be released/reassigned by its original claimant's chain governance path, or documenting/rate-limiting this behavior since it currently allows permanent, irreversible identifier squatting.

### Proof of Concept
1. Victim `V` (address `A_v`) broadcasts a `SetAccountIdContract{account_id = "mycompany", owner_address = A_v}`.
2. Attacker observes this in the mempool and immediately broadcasts `SetAccountIdContract{account_id = "mycompany", owner_address = A_attacker}` with a higher fee/priority so it is included first.
3. `SetAccountIdActuator.validate()` for the attacker's tx succeeds (`accountIdIndexStore.has("mycompany")` is false at that point), and `execute()` commits `"mycompany" → A_attacker` into `AccountIdIndexStore`. [5](#0-4) 
4. When `V`'s original transaction is processed, `validate()` now finds `accountIdIndexStore.has("mycompany")` true and throws `"This id has existed"`, permanently blocking `V` from ever claiming `"mycompany"` — confirmed by the existing `nameAlreadyUsed` unit test's exact assertion path. [6](#0-5) 
5. Any subsequent lookup via `getAccountById("mycompany")` now resolves to `A_attacker`, not `A_v`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/SetAccountIdActuator.java (L45-51)
```java
    byte[] ownerAddress = setAccountIdContract.getOwnerAddress().toByteArray();
    AccountCapsule account = accountStore.get(ownerAddress);

    account.setAccountId(setAccountIdContract.getAccountId().toByteArray());
    accountStore.put(ownerAddress, account);
    accountIdIndexStore.put(account);
    ret.setStatus(fee, code.SUCESS);
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
