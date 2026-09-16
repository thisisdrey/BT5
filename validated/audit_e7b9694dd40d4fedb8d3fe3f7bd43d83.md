### Title
Single-step, unconfirmed Owner-permission transfer in `AccountPermissionUpdateContract` can permanently brick an account - ([File: actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java])

### Summary
`AccountPermissionUpdateActuator` lets an account atomically overwrite its `Owner`, `Witness`, and `Active` permissions — including the set of key addresses that control the account — in a single transaction, with no acceptance/confirmation step from the new key holders. This mirrors the reported "lack of two-step ownership transfer" pattern in `PNG.sol`/`Airdrop.sol`: the only checks performed are address-format validity and threshold/weight arithmetic, not that the designated new controlling address(es) are actually usable/controlled by anyone.

### Finding Description
`execute()` unpacks the `AccountPermissionUpdateContract` and immediately calls `account.updatePermissions(...)`, replacing the account's `Owner` permission (and `Witness`/`Active` permissions) in one step: [1](#0-0) 

`validate()` only checks that key addresses are well-formed (`DecodeUtil.addressValid`), that weights/thresholds are positive integers, and that the weight sum meets the threshold — it never confirms that the new `Owner` key(s) can actually produce a valid signature or that the designated address is reachable/controlled: [2](#0-1) 

Because the `Owner` permission is the top-level permission that gates any future `AccountPermissionUpdateContract` (i.e. the only way to fix a bad permission set is to sign with the *current* `Owner` permission), once this transaction is applied, control of the account is defined solely by the new key set. If the new `Owner` key address is mistyped, belongs to an exchange/custodial deposit address, or otherwise has no accessible private key, there is no fallback: the previous owner keys lose authority and the new, "uncontrolled" address is now nominally in charge, exactly the failure mode the external report describes for `Png::setAdmin`/`Airdrop::setOwner` — a one-shot handoff without any pending/accept step that would let an operator catch a mistake before it becomes irreversible. Unlike Ethereum-style two-step `Ownable2Step`, java-tron has no "pending owner + accept" concept for account permissions.

### Impact Explanation
If the new `Owner` permission's keys are set to an address nobody controls (typo, wrong prefix, custodial/exchange address, burnt key), the account permanently loses the ability to ever again change its own permissions, approve high-value multisig actions gated by `Owner`, or recover — a permanent loss of access to privileged account functions, matching the report's Impact classification.

### Likelihood Explanation
Any account holder can construct and broadcast an `AccountPermissionUpdateContract` transaction directly (multisig setup is a normal, encouraged operation on TRON), so the path is reachable by an ordinary unprivileged transaction sender with no special conditions required. Human error (typo'd address, copy-paste of the wrong key) is the primary trigger, consistent with the "Likelihood: 3" scoring in the source report.

### Recommendation
Introduce an explicit two-step confirmation for `Owner`-permission changes: stage the new permission set and require a follow-up transaction signed by the *new* keys (proving they can produce a valid signature) before the old `Owner` permission is invalidated, analogous to the `Timelock`'s accepted two-step pattern referenced in the report. At minimum, consider requiring the transaction to include a self-signed proof from each new key in the `Owner` permission before committing the update in `AccountPermissionUpdateActuator.execute()`.

### Proof of Concept
1. Account `A` (controlled by key `K_old`) broadcasts an `AccountPermissionUpdateContract` signed by `K_old`, setting the new `Owner` permission's key to address `X` (a mistyped/unowned address) via [3](#0-2) .
2. `validate()` passes because `X` is a well-formed address per `DecodeUtil.addressValid` and the weight/threshold math is satisfied — no check exists that `X` is reachable/controlled: [4](#0-3) .
3. `execute()` commits the new `Owner` permission unconditionally: [5](#0-4) .
4. Any subsequent transaction requiring the `Owner` permission (including another `AccountPermissionUpdateContract` to fix the mistake) now requires a signature from `X`, which nobody can produce — account `A` is permanently locked out of `Owner`-gated operations.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L34-69)
```java
  @Override
  public boolean execute(Object object) throws ContractExeException {
    TransactionResultCapsule result = (TransactionResultCapsule) object;
    if (Objects.isNull(result)) {
      throw new RuntimeException(ActuatorConstant.TX_RESULT_NULL);
    }

    AccountStore accountStore = chainBaseManager.getAccountStore();
    long fee = calcFee();
    final AccountPermissionUpdateContract accountPermissionUpdateContract;
    try {
      accountPermissionUpdateContract = any.unpack(AccountPermissionUpdateContract.class);

      byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
      AccountCapsule account = accountStore.get(ownerAddress);
      account.updatePermissions(accountPermissionUpdateContract.getOwner(),
          accountPermissionUpdateContract.getWitness(),
          accountPermissionUpdateContract.getActivesList());
      accountStore.put(ownerAddress, account);

      adjustBalance(accountStore, ownerAddress, -fee);
      if (chainBaseManager.getDynamicPropertiesStore().supportBlackHoleOptimization()) {
        chainBaseManager.getDynamicPropertiesStore().burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }

      result.setStatus(fee, code.SUCESS);
    } catch (BalanceInsufficientException | InvalidProtocolBufferException e) {
      logger.debug(e.getMessage(), e);
      result.setStatus(fee, code.FAILED);
      throw new ContractExeException(e.getMessage());
    }

    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L105-122)
```java
    for (Key key : permission.getKeysList()) {
      if (!DecodeUtil.addressValid(key.getAddress().toByteArray())) {
        throw new ContractValidateException("key is not a validate address");
      }
      if (key.getWeight() <= 0) {
        throw new ContractValidateException("key's weight should be greater than 0");
      }
      try {
        weightSum = addExact(weightSum, key.getWeight());
      } catch (ArithmeticException e) {
        throw new ContractValidateException(e.getMessage());
      }
    }
    if (weightSum < permission.getThreshold()) {
      throw new ContractValidateException(
          "sum of all key's weight should not be less than threshold in permission " + permission
              .getType());
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/AccountPermissionUpdateActuator.java (L178-185)
```java
    byte[] ownerAddress = accountPermissionUpdateContract.getOwnerAddress().toByteArray();
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("invalidate ownerAddress");
    }
    AccountCapsule accountCapsule = accountStore.get(ownerAddress);
    if (accountCapsule == null) {
      throw new ContractValidateException("ownerAddress account does not exist");
    }
```
