### Title
Transfers of TRX and TRC10 assets to the reserved/never-derivable "zero" address permanently lock funds due to insufficient address validation - (File: `actuator/src/main/java/org/tron/core/actuator/TransferActuator.java`, `actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java`, `common/src/main/java/org/tron/common/utils/DecodeUtil.java`)

### Summary
`DecodeUtil.addressValid` only checks that an address is 21 bytes long and starts with the `0x41` network prefix byte. It never verifies that the remaining 20 bytes are non-zero or otherwise correspond to a derivable key. Any unprivileged broadcaster can send a `TransferContract` or `TransferAssetContract` (and other contracts relying on the same check) to an address such as `0x41` followed by 20 zero bytes. The actuator auto-creates this account and irrevocably moves the sender's TRX/TRC10 balance into it, exactly mirroring the reported Symmetrical bug where `depositFor(address(0), amount)` permanently stranded collateral because there is no way to prove/derive a private key for such a degenerate address.

### Finding Description
`DecodeUtil.addressValid` performs only length and prefix-byte checks: [1](#0-0) 

`TransferActuator.validate()` relies solely on this check for both `ownerAddress` and `toAddress`, with no additional check against a "null"/all-zero address body: [2](#0-1) 

`TransferActuator.execute()` then unconditionally creates a new account for any previously-unseen `toAddress` and credits it with the transferred amount: [3](#0-2) 

The same pattern (address-format-only validation, followed by auto-account-creation and balance credit) is repeated in `TransferAssetActuator` for TRC10 tokens: [4](#0-3) [5](#0-4) 

Because an address of the form `0x41 + 20×0x00` has no known/derivable ECDSA private key (unlike TRON's dedicated black-hole/burn address, which is an intentional, publicly documented sink), any balance sent there is permanently unrecoverable — there is no `withdraw`-style operation that can move funds out of an account nobody controls the key for. This is a direct structural analog to the reported issue: `AccountFacetImpl.deposit` in the Symmetrical contracts records a balance for `address(0)` with no possibility of `withdraw`/`withdrawTo` recovering it, because `address(0)` cannot receive collateral and has no controlling key.

### Impact Explanation
Any unprivileged transaction broadcaster (regular user, wallet software, or a bug in an integrating exchange/service) can accidentally or through a malicious front-end/typo send TRX or TRC10 tokens to this degenerate address, resulting in a concrete, permanent, unrecoverable loss of funds — a freezing-of-funds condition matching the accepted High-severity impact criteria (permanent freezing/unbacked balance for the sender, unlike the intentional black-hole burn address that the protocol explicitly uses for fee-burning).

### Likelihood Explanation
Likelihood is non-trivial: this requires only a single, unprivileged `TransferContract` or `TransferAssetContract` transaction — no special privileges, no contract deployment, no auxiliary conditions. It is the same reachable-by-anyone precondition emphasized in the analog report (a single call from `msg.sender`). The main mitigating factor is that a user or wallet must supply this specific degenerate address, which is not the default in most wallets, but nothing in the actuator prevents it.

### Recommendation
Extend `DecodeUtil.addressValid` (or add explicit checks in `TransferActuator.validate()`, `TransferAssetActuator.validate()`, and other actuators/native contract processors that accept a destination/receiver address) to reject the all-zero address body (i.e., `0x41` followed by 20 zero bytes), similar to how `Arrays.equals(toAddress, ownerAddress)` is already explicitly rejected in `TransferActuator.validate()`: [6](#0-5) 

### Proof of Concept
1. Construct a `TransferContract` (or `TransferAssetContract`) with `toAddress` set to `0x41` + 20 zero bytes (`417000000000000000000000000000000000000000` is invalid length-wise; the exact valid-length degenerate value is `0x41` followed by 20×`0x00`).
2. Broadcast the signed transaction from any funded account.
3. `TransferActuator.validate()` passes both `addressValid` checks since only length/prefix are verified.
4. `TransferActuator.execute()` creates a new `AccountCapsule` for the zero-body address and credits it with the transferred amount via `adjustBalance(accountStore, toAddress, amount)`.
5. Because no private key can be derived for this address, the credited balance can never be moved again — it is permanently locked, analogous to the reported `depositFor(address(0), amount)` issue.

### Citations

**File:** common/src/main/java/org/tron/common/utils/DecodeUtil.java (L15-33)
```java
  public static boolean addressValid(byte[] address) {
    if (ArrayUtils.isEmpty(address)) {
      logger.warn("Warning: Address is empty !!");
      return false;
    }
    if (address.length != ADDRESS_SIZE / 2) {
      logger.warn(
          "Warning: Address length need " + ADDRESS_SIZE + " but " + address.length
              + " !!");
      return false;
    }

    if (address[0] != addressPreFixByte) {
      logger.warn("Warning: Address need prefix with " + addressPreFixByte + " but "
          + address[0] + " !!");
      return false;
    }
    return true;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L48-66)
```java
      // if account with to_address does not exist, create it first.
      AccountCapsule toAccount = accountStore.get(toAddress);
      if (toAccount == null) {
        boolean withDefaultPermission =
            dynamicStore.getAllowMultiSign() == 1;
        toAccount = new AccountCapsule(ByteString.copyFrom(toAddress), AccountType.Normal,
            dynamicStore.getLatestBlockHeaderTimestamp(), withDefaultPermission, dynamicStore);
        accountStore.put(toAddress, toAccount);

        fee = fee + dynamicStore.getCreateNewAccountFeeInSystemContract();
      }

      adjustBalance(accountStore, ownerAddress, -(addExact(fee, amount)));
      if (dynamicStore.supportBlackHoleOptimization()) {
        dynamicStore.burnTrx(fee);
      } else {
        adjustBalance(accountStore, accountStore.getBlackhole(), fee);
      }
      adjustBalance(accountStore, toAddress, amount);
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L100-113)
```java
    byte[] toAddress = transferContract.getToAddress().toByteArray();
    byte[] ownerAddress = transferContract.getOwnerAddress().toByteArray();
    long amount = transferContract.getAmount();

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress!");
    }
    if (!DecodeUtil.addressValid(toAddress)) {
      throw new ContractValidateException("Invalid toAddress!");
    }

    if (Arrays.equals(toAddress, ownerAddress)) {
      throw new ContractValidateException("Cannot transfer TRX to yourself.");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L60-71)
```java
      byte[] ownerAddress = transferAssetContract.getOwnerAddress().toByteArray();
      byte[] toAddress = transferAssetContract.getToAddress().toByteArray();
      AccountCapsule toAccountCapsule = accountStore.get(toAddress);
      if (toAccountCapsule == null) {
        boolean withDefaultPermission =
            dynamicStore.getAllowMultiSign() == 1;
        toAccountCapsule = new AccountCapsule(ByteString.copyFrom(toAddress), AccountType.Normal,
            dynamicStore.getLatestBlockHeaderTimestamp(), withDefaultPermission, dynamicStore);
        accountStore.put(toAddress, toAccountCapsule);

        fee = fee + dynamicStore.getCreateNewAccountFeeInSystemContract();
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L130-141)
```java
    long fee = calcFee();
    byte[] ownerAddress = transferAssetContract.getOwnerAddress().toByteArray();
    byte[] toAddress = transferAssetContract.getToAddress().toByteArray();
    byte[] assetName = transferAssetContract.getAssetName().toByteArray();
    long amount = transferAssetContract.getAmount();

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress");
    }
    if (!DecodeUtil.addressValid(toAddress)) {
      throw new ContractValidateException("Invalid toAddress");
    }
```
