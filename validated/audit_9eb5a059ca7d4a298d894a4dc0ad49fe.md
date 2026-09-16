### Title
Missing zero/burn-address check on `toAddress` in `TransferActuator` and `TransferAssetActuator` allows permanent, irrecoverable loss of TRX/TRC10 funds - (File: actuator/src/main/java/org/tron/core/actuator/TransferActuator.java)

### Summary
`TransferActuator.validate()` and `TransferAssetActuator.validate()` only verify the recipient address's *format* via `DecodeUtil.addressValid()`, which checks length and the network prefix byte, but never checks whether the address is the degenerate all-zero address (`0x41` followed by 20 zero bytes). Because `TransferActuator.execute()` auto-creates the destination account if it does not already exist and then unconditionally moves the balance/asset there, a transaction sending value to this address is accepted and the funds become permanently unrecoverable, mirroring the audit report's root cause: no check that the receiver is not the zero address before value is moved to it.

### Finding Description
`DecodeUtil.addressValid()` performs only two checks: non-empty/length == 21 bytes, and the first byte equals the chain's address prefix (`0x41` on mainnet). It never rejects the address consisting of the prefix byte followed by all zero bytes: [1](#0-0) 

`TransferActuator.validate()` uses this same weak check for `toAddress` and only additionally rejects self-transfers; it does not exclude the zero address: [2](#0-1) 

`TransferActuator.execute()` then auto-creates the account at `toAddress` if it doesn't yet exist and moves the TRX balance there unconditionally: [3](#0-2) 

The same pattern exists in `TransferAssetActuator.validate()` for TRC10 token transfers - `toAddress` is validated only for format, not for being the zero/burn address: [4](#0-3) 

By contrast, other value-moving actuators in the same codebase (`DelegateResourceActuator`, `UnfreezeBalanceActuator`, `FreezeBalanceActuator`) require that a `receiverAddress` correspond to an *existing* account before any value is delegated, which incidentally blocks accidental transfers to a never-used zero address: [5](#0-4) 

`TransferActuator`/`TransferAssetActuator` lack this "receiver must already exist" safety net — they explicitly support and auto-create brand-new destination accounts, which is precisely the code path that permits an accidental or malformed zero-value receiver to silently succeed and permanently lock funds, with no path in the protocol to reclaim them (no private key exists for the literal zero address, and it is distinct from the deliberate protocol black-hole address used for fee burns).

### Impact Explanation
Any signed `TransferContract` (TRX) or `TransferAssetContract` (TRC10) transaction that supplies the zero address (`41` + 20 zero bytes) as `toAddress` will be validated and executed successfully by the node, moving TRX balance or token balance to an address for which no one holds a private key. This constitutes a permanent, irrecoverable loss of funds for the transaction sender — matching the "permanent freezing of funds"/"unbacked balance" impact criteria. Because these are the primary, most heavily used broadcastable transaction types on java-tron, any client, exchange, wallet integration bug, or malformed input (e.g., an uninitialized/zeroed address field from a buggy signer, batch script, or bridge integration) that slips past client-side checks would result in silent, permanent fund loss at the protocol layer instead of a validation rejection.

### Likelihood Explanation
Likelihood is moderate: this requires either user/integration error (a caller populating `toAddress` with a zero-filled byte array, e.g. from an unset field, bug in a signing library, or malformed relayer/bridge code) or of course a deliberate self-inflicted send. It does not require any privileged role — any account broadcaster can trigger this path with a single signed transaction. Given the volume of automated infrastructure (exchanges, bridges, batch payout scripts) integrating with java-tron nodes, uninitialized/zeroed `toAddress` fields are a realistic failure mode that the protocol currently does nothing to guard against, unlike protocols that explicitly reject the zero address as a defense-in-depth measure.

### Recommendation
Add an explicit check in both `TransferActuator.validate()` and `TransferAssetActuator.validate()` (and any other actuator that accepts a caller-supplied destination address and can auto-create/fund a new account) that rejects `toAddress` when it equals the all-zero address (i.e., prefix byte followed by 20 zero bytes), in addition to the existing format and self-transfer checks. Consider centralizing this check inside `DecodeUtil.addressValid()` or adding a dedicated `isZeroAddress()` utility used consistently across all fund-moving actuators.

### Proof of Concept
1. Construct a `TransferContract` with `ownerAddress` = a funded account and `toAddress` = `0x41` followed by 20 zero bytes (`4100000000000000000000000000000000000000`).
2. Sign and broadcast the transaction.
3. `TransferActuator.validate()` passes: `DecodeUtil.addressValid(toAddress)` returns `true` (correct length/prefix), and `Arrays.equals(toAddress, ownerAddress)` is `false`. [6](#0-5) 
4. `TransferActuator.execute()` creates a new `AccountCapsule` at the zero address (since none exists) and credits it with the transferred amount: [7](#0-6) 
5. The funds are now held by an address for which no private key exists, and there is no protocol mechanism to move them out — permanent loss. The identical flow applies to `TransferAssetActuator` for TRC10 token balances.

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

**File:** actuator/src/main/java/org/tron/core/actuator/TransferAssetActuator.java (L136-149)
```java
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid ownerAddress");
    }
    if (!DecodeUtil.addressValid(toAddress)) {
      throw new ContractValidateException("Invalid toAddress");
    }

    if (amount <= 0) {
      throw new ContractValidateException("Amount must be greater than 0.");
    }

    if (Arrays.equals(ownerAddress, toAddress)) {
      throw new ContractValidateException("Cannot transfer asset to yourself.");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/DelegateResourceActuator.java (L203-209)
```java
    AccountCapsule receiverCapsule = accountStore.get(receiverAddress);
    if (receiverCapsule == null) {
      String readableOwnerAddress = StringUtil.createReadableString(receiverAddress);
      throw new ContractValidateException(
          ActuatorConstant.ACCOUNT_EXCEPTION_STR
              + readableOwnerAddress + NOT_EXIST_STR);
    }
```
