### Title
Missing zero-address validation in transfer-related actuators enables permanent freezing of TRX/TRC10 funds - (File: `common/src/main/java/org/tron/common/utils/DecodeUtil.java`)

### Summary
`DecodeUtil.addressValid()`, the single address-validation routine reused by all balance-moving actuators (`TransferActuator`, `TransferAssetActuator`, `ParticipateAssetIssueActuator`, `ShieldedTransferActuator`, and the smart-contract transfer path via `VMUtils.validateForSmartContract`/`MUtil.transfer`), only checks that an address has the correct length and the correct chain prefix byte. It never rejects the all-zero address body (`prefix + 20 zero bytes`), which is a syntactically valid TRON address that has no known controlling private key.

### Finding Description
`addressValid` performs exactly two checks — non-empty/length-21 and correct prefix byte — and returns `true` for any 21-byte array meeting those two conditions, including the "zero" address: [1](#0-0) 

This helper is the sole gate used to validate `toAddress` in the core transfer paths reachable directly from a signed transaction:
- `TransferActuator.validate()` calls `DecodeUtil.addressValid(toAddress)` and, if it passes, proceeds to create the destination account and move TRX to it: [2](#0-1) [3](#0-2) 
- `TransferAssetActuator.validate()` performs the identical check for TRC10 asset transfers: [4](#0-3) 
- The TVM internal-transfer path (`CALL`/`TRANSFER` opcodes reaching `MUtil.transfer`/`MUtil.transferToken`) reuses `VMUtils.validateForSmartContract`, which applies the same weak check before moving TRX/tokens between accounts inside contract execution: [5](#0-4) [6](#0-5) 

In all of these, the "self-transfer" guard (`Arrays.equals(toAddress, ownerAddress)`) is present, but there is no guard against sending to the zero address, which is analogous to the reported `Funding.sol` issue: the destination is format-valid but functionally uncontrollable.

### Impact Explanation
Any TRX, TRC10 asset, or contract-internal transfer sent to the zero-body address (`{prefix}0000...0000`) is accepted by validation and executed, permanently locking the transferred funds since no party can produce a private key for that address in practice (analogous to Ethereum's well-known zero-address burn pattern). This is directly reachable by any unprivileged transaction broadcaster or contract caller — no special privilege is required, matching the "permanent freezing of funds" impact class.

### Likelihood Explanation
Likelihood is driven by user/dApp error (malformed address construction, off-by-bug frontends, bugged batch scripts) rather than by an attacker profiting, but the transaction is fully valid on-chain and irreversibly locks funds with a single signed `TransferContract`/`TransferAssetContract`/contract call — a broadcaster does not need any special role, only a normal signed transaction.

### Recommendation
Add an explicit zero-address rejection alongside the existing prefix/length check in `DecodeUtil.addressValid()` (or at each call site immediately after it), e.g. verifying the 20-byte body is not all zero, mirroring the existing self-transfer guards already present in `TransferActuator`, `TransferAssetActuator`, and `VMUtils`.

### Proof of Concept
1. Construct `toAddress = {ADD_PRE_FIX_BYTE_MAINNET} + 20 zero bytes` (21 bytes total, correct length and prefix).
2. Submit a `TransferContract` transaction with this `toAddress` and a nonzero `amount`.
3. `TransferActuator.validate()` passes because `DecodeUtil.addressValid(toAddress)` returns `true` and `Arrays.equals(toAddress, ownerAddress)` is `false`.
4. `TransferActuator.execute()` creates a new account at the zero-body address and irreversibly credits it with `amount`, permanently removing those funds from circulation/control. [7](#0-6)

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

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L45-66)
```java
      byte[] toAddress = transferContract.getToAddress().toByteArray();
      byte[] ownerAddress = transferContract.getOwnerAddress().toByteArray();

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

**File:** actuator/src/main/java/org/tron/core/actuator/TransferActuator.java (L100-109)
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L136-141)
```java
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress");
    }
    if (!DecodeUtil.addressValid(toAddress)) {
      throw new ContractValidateException("Invalid toAddress");
    }
```

**File:** actuator/src/main/java/org/tron/core/vm/VMUtils.java (L137-148)
```java
  public static boolean validateForSmartContract(Repository deposit, byte[] ownerAddress,
      byte[] toAddress, long amount) throws ContractValidateException {
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

**File:** actuator/src/main/java/org/tron/core/vm/utils/MUtil.java (L18-26)
```java
  public static void transfer(Repository deposit, byte[] fromAddress, byte[] toAddress, long amount)
      throws ContractValidateException {
    if (0 == amount) {
      return;
    }
    VMUtils.validateForSmartContract(deposit, fromAddress, toAddress, amount);
    deposit.addBalance(toAddress, amount);
    deposit.addBalance(fromAddress, -amount);
  }
```
