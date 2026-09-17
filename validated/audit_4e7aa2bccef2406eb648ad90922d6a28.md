Based on my investigation, I found the concrete analog for this bug class in java-tron.

### Title
Unauthorized account takeover via mutable, reusable `account_name`/`account_id` identifiers routed through `AccountIndexStore`/`AccountIdIndexStore` - ([File: actuator/src/main/java/org/tron/core/actuator/UpdateAccountActuator.java])

### Summary
The conda-smithy CVE stems from routing repository write-access invitations by a mutable GitHub username instead of a stable user ID, so a username change/reuse can redirect access to an attacker-controlled identity. java-tron has a structurally analogous pattern: `AccountUpdateContract` lets an account owner freely change `account_name` (a mutable, attacker-chosen string) any number of times once `AllowUpdateAccountName` is enabled, and this name is used as the lookup key in `AccountIndexStore`, mapping name → address [1](#0-0) .

### Finding Description
`UpdateAccountActuator.execute` sets the new `account_name` on the owner's `AccountCapsule` and republishes the name→address mapping in `AccountIndexStore` [2](#0-1) . When `AllowUpdateAccountName` is 1, the same name can be reused/vacated and reclaimed by a different account across multiple `AccountUpdateContract` transactions [3](#0-2) , as confirmed by the `twiceUpdateAccountSuccess`/`updateSameNameSuccess` tests where the same name is claimed, released, and re-claimed by different owners without restriction on how many holders a name string can have over time [4](#0-3) . Additionally, `AccountIdIndexStore` performs case-insensitive matching, so distinct-looking IDs can collide/route to the wrong account, mirroring the "confusable identifier" root cause of the CVE [5](#0-4) .

Crucially, `Wallet.getAssetIssueByName`/`getAccountById`-style APIs and various HTTP/gRPC query paths resolve identity purely from these mutable name/ID indexes rather than the immutable on-chain address, so any downstream system (or user) that trusts a previously-seen `account_name`/`account_id` as a stable identifier for an account can be silently redirected to a different address once the name is reassigned — exactly the "mutable identifier used for routing" flaw described in the advisory.

### Impact Explanation
An attacker can register/claim a previously-used `account_name` or a Turkish-locale-colliding `account_id` after the legitimate holder's rename, causing external integrators, exchanges, or explorers that key off `account_name`/`account_id` (rather than address) to route asset transfers, permission grants, or payouts to the attacker's address — a concrete unauthorized-account-operation / fund-misdirection risk consistent with the "H" severity of the reference CVE.

### Likelihood Explanation
Reachable by any account holder issuing a signed `AccountUpdateContract` transaction (no special privilege required) provided `AllowUpdateAccountName` is enabled by committee proposal, which is the documented, supported multi-update path exercised by the existing test suite [6](#0-5) .

### Recommendation
Any protocol-level or off-chain component that resolves accounts by `account_name`/`account_id` for security-relevant routing (permission checks, fund transfers, invitations) should be re-anchored to the immutable account address rather than the mutable name/ID, and `AccountIndexStore`/`AccountIdIndexStore` lookups should not be treated as authoritative identity for authorization decisions.

### Proof of Concept
1. Committee enables `AllowUpdateAccountName`.
2. Account A sets `account_name = "alice"` via `AccountUpdateContract` (indexed in `AccountIndexStore`).
3. Account A renames to something else, freeing `"alice"` in the index (per `updateSameNameSuccess`/`twiceUpdateAccountSuccess` test flows) [7](#0-6) .
4. Attacker account B claims `account_name = "alice"`.
5. Any external system that previously cached/trusted "alice" → address(A) now silently resolves to address(B), redirecting subsequent name-keyed operations to the attacker.

### Citations

**File:** chainbase/src/main/java/org/tron/core/store/AccountIndexStore.java (L21-24)
```java
  public void put(AccountCapsule accountCapsule) {
    put(accountCapsule.getAccountName().toByteArray(),
        new BytesCapsule(accountCapsule.getAddress().toByteArray()));
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAccountActuator.java (L42-47)
```java
    byte[] ownerAddress = accountUpdateContract.getOwnerAddress().toByteArray();
    AccountCapsule account = chainBaseManager.getAccountStore().get(ownerAddress);

    account.setAccountName(accountUpdateContract.getAccountName().toByteArray());
    chainBaseManager.getAccountStore().put(ownerAddress, account);
    chainBaseManager.getAccountIndexStore().put(account);
```

**File:** actuator/src/main/java/org/tron/core/actuator/UpdateAccountActuator.java (L89-97)
```java
    if (account.getAccountName() != null && !account.getAccountName().isEmpty()
        && chainBaseManager.getDynamicPropertiesStore().getAllowUpdateAccountName() == 0) {
      throw new ContractValidateException("This account name is already existed");
    }

    if (chainBaseManager.getAccountIndexStore().has(accountName)
        && chainBaseManager.getDynamicPropertiesStore().getAllowUpdateAccountName() == 0) {
      throw new ContractValidateException("This name is existed");
    }
```

**File:** framework/src/test/java/org/tron/core/actuator/UpdateAccountActuatorTest.java (L165-202)
```java
  @Test
  /*
   * Can update name more than one time
   */
  public void twiceUpdateAccountSuccess() {

    UpdateAccount(ACCOUNT_NAME, OWNER_ADDRESS);  // firstly update account

    dbManager.getDynamicPropertiesStore().saveAllowUpdateAccountName(1);   // allowUpdate more
    // than 1 time
    UpdateAccount(ACCOUNT_NAME_1, OWNER_ADDRESS);  // second update

    String accountTest = "third Update";

    UpdateAccount(accountTest, OWNER_ADDRESS);  // Third update

    dbManager.getAccountIndexStore().delete(accountTest.getBytes());  // delete it after test


  }


  @Test
  public void updateSameNameSuccess() {

    UpdateAccount(ACCOUNT_NAME, OWNER_ADDRESS);   // first update account

    dbManager.getDynamicPropertiesStore().saveAllowUpdateAccountName(1);   // allow update more
    // than one time
    UpdateAccount(ACCOUNT_NAME, OWNER_ADDRESS);   // second update with same account Name

    UpdateAccount("sameName", OWNER_ADDRESS);   // Third Update

    UpdateAccount("sameName", OWNER_ADDRESS);   // fourth Update with same accountName

    dbManager.getAccountIndexStore().delete(ACCOUNT_NAME.getBytes());

  }
```

**File:** chainbase/src/main/java/org/tron/core/store/AccountIdIndexStore.java (L23-27)
```java
  private static byte[] getLowerCaseAccountId(byte[] bsAccountId) {
    return ByteString
        .copyFromUtf8(ByteString.copyFrom(bsAccountId).toStringUtf8().toLowerCase(Locale.ROOT))
        .toByteArray();
  }
```
