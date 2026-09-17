### Title
Unchecked null return from `Repository.getContract()` causes NullPointerException during internal CALL execution - (File: actuator/src/main/java/org/tron/core/vm/program/Program.java)

### Summary
In `Program.callToAddress()`, when a smart contract issues an internal `CALL`/`CALLCODE`/`DELEGATECALL`/`STATICCALL` to another address and the TVM-EVM-compatibility feature is enabled, the code dereferences the result of `invoke.getDeposit().getContract(codeAddress)` without checking for `null`, exactly the same bug class as the reported rtw89 issue (`ieee80211_probereq_get()` return value dereferenced without a null check).

### Finding Description
`Program.callToAddress()` fetches the callee's bytecode via `getContractState().getCode(codeAddress)` and only enters the `isNotEmpty(programCode)` branch when code exists for that address. [1](#0-0) 
Inside that branch, when the `AllowTvmCompatibleEvm` proposal is active, the code directly chains `.getContractVersion()` onto the return value of `invoke.getDeposit().getContract(codeAddress)` with no null check: [2](#0-1) 

`RepositoryImpl.getContract(byte[] address)` explicitly can return `null` when there is no `ContractCapsule` entry for the address in the `ContractStore` (and no cached value up the parent-repository chain): [3](#0-2) 

Meanwhile, `RepositoryImpl.getCode(byte[] address)` reads bytecode from a completely separate store (`CodeStore`/`codeCache`), independent of `ContractStore`: [4](#0-3) 

Because `getCode()` and `getContract()` query different underlying stores, any state where an address has bytecode in `CodeStore` but no corresponding `ContractCapsule` in `ContractStore` will cause `getContract(codeAddress)` to return `null` at line 1157, and the subsequent `.getContractVersion()` call throws an uncaught `NullPointerException`. Other call sites in the same file that read `getContract()` for similar purposes are careful to null-check the result before use, e.g. `getCodeHashAt()` and `isContract()`: [5](#0-4) [6](#0-5) 
This confirms that line 1157 is the outlier that omits the standard defensive null check used elsewhere in the same class for the exact same accessor.

### Impact Explanation
An uncaught `NullPointerException` thrown deep inside TVM opcode execution (`callToAddress`) during transaction processing is not one of the checked/expected exception types handled by the actuator's controlled exception paths (`ContractValidateException`/`ContractExeException`/VM `Exception` hierarchy). If this path is reached during block application (not just a constant/eth_call), it can abort processing of a transaction in a way that is inconsistent across nodes, or crash/halt node processing of that block, satisfying the "node crash or halt" acceptance criterion.

### Likelihood Explanation
The trigger requires: (1) the `AllowTvmCompatibleEvm` proposal to be enabled (a network-wide, committee-approved feature that is deployed on both mainnet and testnets), and (2) an address reachable via `CALL`/`CALLCODE`/`DELEGATECALL`/`STATICCALL` whose bytecode exists in `CodeStore` but has no matching `ContractCapsule` in `ContractStore`. Whether such a state is reachable purely from an external, unprivileged transaction (i.e., without direct DB manipulation) could not be fully confirmed with the available tooling — normal contract deployment and self-destruct paths (`RepositoryImpl.createContract`/`saveCode`/`deleteContract`) keep the two stores in sync. I was not able to trace every corner-case (e.g., partial commits during nested/reverted internal CREATE calls, version-1 contract upgrade/migration paths) that could desynchronize `CodeStore` from `ContractStore` before the tool budget was exhausted, so likelihood should be treated as uncertain rather than confirmed.

### Recommendation
Add a null check for `invoke.getDeposit().getContract(codeAddress)` before calling `.getContractVersion()` in `Program.callToAddress()`, mirroring the pattern already used in `getCodeHashAt()`/`isContract()` in the same file, and fall back to a safe default contract version (e.g., treat as version 0 / non-EVM-compatible) when no `ContractCapsule` exists for the address.

### Proof of Concept
Conceptual PoC (exact reachability of the desync state not fully verified):
1. Enable the `AllowTvmCompatibleEvm` chain parameter (already active on chains where this proposal has passed).
2. Deploy or otherwise obtain an address `X` where `CodeStore` contains non-empty bytecode for `X` but `ContractStore` has no `ContractCapsule` entry for `X` (the exact transaction sequence able to desynchronize the two stores was not confirmed within this investigation).
3. Deploy/trigger a contract `A` that issues `CALL`/`DELEGATECALL`/`STATICCALL` to address `X`.
4. During `Program.callToAddress()`, `getContractState().getCode(X)` returns non-empty bytecode so the `isNotEmpty(programCode)` branch executes, and `invoke.getDeposit().getContract(X)` returns `null`, causing `.getContractVersion()` to throw `NullPointerException`, aborting/crashing execution of the enclosing transaction/block processing.

### Citations

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1079-1082)
```java
    AccountCapsule accountCapsule = getContractState().getAccount(codeAddress);

    byte[] programCode =
        accountCapsule != null ? getContractState().getCode(codeAddress) : EMPTY_BYTE_ARRAY;
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1155-1158)
```java
      if (VMConfig.allowTvmCompatibleEvm()) {
        program.setContractVersion(invoke.getDeposit()
            .getContract(codeAddress).getContractVersion());
      }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1307-1328)
```java
  public byte[] getCodeHashAt(DataWord address) {
    byte[] tronAddr = address.toTronAddress();
    AccountCapsule account = getContractState().getAccount(tronAddr);
    if (account != null) {
      ContractCapsule contract = getContractState().getContract(tronAddr);
      byte[] codeHash;
      if (contract != null) {
        codeHash = contract.getCodeHash();
        if (ByteUtil.isNullOrZeroArray(codeHash)) {
          byte[] code = getCodeAt(address);
          codeHash = Hash.sha3(code);
          contract.setCodeHash(codeHash);
          getContractState().updateContract(tronAddr, contract);
        }
      } else {
        codeHash = Hash.sha3(new byte[0]);
      }
      return codeHash;
    } else {
      return EMPTY_BYTE_ARRAY;
    }
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/program/Program.java (L1384-1387)
```java
  public DataWord isContract(DataWord address) {
    ContractCapsule contract = getContractState().getContract(address.toTronAddress());
    return contract != null ? DataWord.ONE() : DataWord.ZERO();
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L501-519)
```java
  @Override
  public ContractCapsule getContract(byte[] address) {
    Key key = Key.create(address);
    if (contractCache.containsKey(key)) {
      return new ContractCapsule(contractCache.get(key).getValue());
    }

    ContractCapsule contractCapsule;
    if (parent != null) {
      contractCapsule = parent.getContract(address);
    } else {
      contractCapsule = getContractStore().get(address);
    }

    if (contractCapsule != null) {
      contractCache.put(key, Value.create(contractCapsule));
    }
    return contractCapsule;
  }
```

**File:** actuator/src/main/java/org/tron/core/vm/repository/RepositoryImpl.java (L673-694)
```java
  @Override
  public byte[] getCode(byte[] address) {
    Key key = Key.create(address);
    if (codeCache.containsKey(key)) {
      return codeCache.get(key).getValue();
    }

    byte[] code;
    if (parent != null) {
      code = parent.getCode(address);
    } else {
      if (null == getCodeStore().get(address)) {
        code = null;
      } else {
        code = getCodeStore().get(address).getData();
      }
    }
    if (code != null) {
      codeCache.put(key, Value.create(code));
    }
    return code;
  }
```
