## Title
Sandwich-attack on `ExchangeInjectContract`/`ExchangeWithdrawContract` due to missing slippage protection — ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java])

### Summary
The `BondVault` bug matches a user-deposited token amount with a `BASE` amount computed from the *current, manipulable* spot price of a pool, with no bound/TWAP check, letting an attacker sandwich the deposit to extract value. java-tron's built-in `Exchange` (Bancor-style AMM) feature has the same structural flaw: `ExchangeInjectContract` and `ExchangeWithdrawContract` compute the matched "another token" amount directly from the exchange's current reserve ratio at execution time, with **no minimum/maximum ("expected") bound field in the contract**, unlike `ExchangeTransactionContract` which does carry an `expected` slippage guard.

### Finding Description
`ExchangeInjectActuator.execute` computes the counterpart amount purely from the live reserves: [1](#0-0) 
and the protobuf message carries no expected/min-amount field to bound this calculation: [2](#0-1) 
Contrast this with `ExchangeTransactionContract`, which does include an `expected` field checked in `doValidate`: [3](#0-2) 
Crucially, `ExchangeTransactionActuator` (the swap operation) can be called by **any account**, not just the exchange creator — its `doValidate` only checks address validity, account existence, and fee balance: [4](#0-3) 
while `ExchangeInjectActuator`/`ExchangeWithdrawActuator` are restricted to the exchange creator: [5](#0-4) 

This means any anonymous account can freely trade against an `Exchange` pool via `ExchangeTransactionContract` to skew the `firstTokenBalance`/`secondTokenBalance` ratio immediately before a pending `ExchangeInjectContract` (or `ExchangeWithdrawContract`) transaction from the exchange creator executes, then reverse the trade afterward — a classic sandwich attack. Because `ExchangeInjectContract`/`ExchangeWithdrawContract` have no `expected`/bound parameter, the actuator will silently compute and commit whatever `anotherTokenQuant` results from the manipulated ratio, transferring value from the injecting/withdrawing party to the attacker who reverses the price move.

### Impact Explanation
An attacker can extract funds from any exchange creator who injects or withdraws liquidity, without needing any special privilege — this is a direct unauthorized value transfer (theft of funds) reachable purely through public, unprivileged `ExchangeTransactionContract` and `ExchangeInjectContract`/`ExchangeWithdrawContract` broadcasts. Since `Exchange` pools can hold TRX and TRC10 assets, losses are real on-chain assets, matching the report's "theft of funds via manipulated matched deposit."

### Likelihood Explanation
The attack requires only observing a pending inject/withdraw transaction from a known exchange creator and being able to place transactions immediately before/after it — a standard front-running/sandwich pattern with no additional privilege needed (all three actuators are open to any signer, with only creator-restriction on the injected/withdrawn side, not on the swap side used to manipulate price). The cost is limited to trading fees and any temporary capital used to skew reserves (no explicit trading fee for exchange contracts, as `calcFee()` returns 0 for these actuators), making the attack cheap relative to potential gains.

### Recommendation
Add an `expected`/bound field (min/max acceptable `anotherTokenQuant`) to `ExchangeInjectContract` and `ExchangeWithdrawContract`, and enforce it in `doValidate`/`execute`, mirroring the protection already present in `ExchangeTransactionContract`. Alternatively, track and require execution within a bounded deviation from a TWAP of the exchange's reserve ratio before allowing inject/withdraw.

### Proof of Concept
1. Attacker monitors the network for a pending `ExchangeInjectContract` (or `ExchangeWithdrawContract`) transaction from exchange creator `C` for exchange `E` (firstToken/secondToken reserves `A`/`B`).
2. Attacker broadcasts an `ExchangeTransactionContract` swap against `E`, dumping a large amount of `firstToken` to skew the ratio (`A` up, `B` down), executed via `ExchangeTransactionActuator.execute`: [6](#0-5) 
3. `C`'s inject transaction then executes using the now-skewed reserves in `ExchangeInjectActuator.execute` at lines 71-83, forcing `C` to commit a disproportionate amount of `secondToken` (or, for withdraw, causing `C` to receive less than the fair share) with no `expected` bound to reject the unfavorable rate.
4. Attacker submits a reverse `ExchangeTransactionContract` swap, restoring the ratio and extracting the excess `secondToken` that `C`'s inject just added to the pool (or that `C`'s withdraw left behind), realizing a profit equal to `C`'s loss minus swap fees.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-83)
```java
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            secondTokenBalance, tokenQuant), firstTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, tokenQuant),
            addExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            firstTokenBalance, tokenQuant), secondTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, anotherTokenQuant),
            addExact(secondTokenBalance, tokenQuant));
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L17-29)
```text
message ExchangeInjectContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}

message ExchangeWithdrawContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L64-75)
```java
      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L142-157)
```java
    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }

    if (!accountStore.has(ownerAddress)) {
      throw new ContractValidateException("account[" + readableOwnerAddress + NOT_EXIST_STR);
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);

    if (accountCapsule.getBalance() < calcFee()) {
      throw new ContractValidateException("No enough balance for exchange transaction fee!");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
