### Title
Missing null-check for unregistered `ContractType` causes uncaught `NullPointerException` in `ActuatorFactory` - ([File: actuator/src/main/java/org/tron/core/actuator/ActuatorFactory.java])

### Summary
`ActuatorFactory.getActuatorByContract()` looks up the actuator implementation class for a transaction's `Contract` via `TransactionFactory.getActuator(contract.getType())`, but — unlike the parallel `ActuatorCreator.getActuatorByContract()` implementation — it never checks whether the lookup returned `null` before calling `clazz.newInstance()`. This is the same "no entry, no checks" pattern as the reported `OperationRegistry.getOperation()` issue: a registry miss silently falls through instead of being explicitly rejected.

### Finding Description
`TransactionFactory` stores actuator classes in a `ConcurrentHashMap<ContractType, Class<? extends Actuator>>` populated only for `ContractType` values that have a registered `AbstractActuator` subclass: [1](#0-0) 

`ActuatorCreator` (used from `Manager.java`) correctly guards against an unregistered type: [2](#0-1) 

`ActuatorFactory` (used from `Wallet.java`) performs the identical lookup but omits the null check entirely: [3](#0-2) 

If `contract.getType()` resolves to a `ContractType` that has no registered actuator class (e.g. an `UNRECOGNIZED`/out-of-range enum value crafted by an attacker in a raw protobuf `Transaction.Contract`, or any future contract type omitted from actuator registration), `TransactionFactory.getActuator()` returns `null`, and `clazz.newInstance()` throws a `NullPointerException`. The enclosing `forEach` lambda in `createActuator()` only catches `IllegalAccessException | InstantiationException`: [4](#0-3) 

Because `NullPointerException` is unchecked and not covered by that catch clause, it propagates out of the lambda and out of `createActuator()` uncaught.

### Impact Explanation
An uncaught `NullPointerException` thrown from deep inside transaction-processing code invoked from `Wallet` (a client/API-facing entry point) can abort the request path unexpectedly instead of returning a clean validation error, and — depending on which caller in `Wallet.java` invokes this path without a surrounding safety net — could destabilize request handling for that API call. This matches the report's underlying concern: absence of an explicit "no entry" check causes downstream logic to run on bad/empty data instead of being rejected up front.

### Likelihood Explanation
Reachability depends on whether a caller can get a `Transaction.Contract` with a `ContractType` that has no corresponding registered actuator into `ActuatorFactory.createActuator()`. This requires: (1) confirming which `Wallet.java` method(s) call `ActuatorFactory.createActuator()` (found 3 references, not fully traced within the available search budget), and (2) confirming whether protobuf enum handling truly allows an `UNRECOGNIZED`/unmapped `ContractType` to reach this code path without being rejected earlier by contract-type validation elsewhere in the broadcast pipeline. I was not able to fully verify the exact `Wallet.java` call site(s) and whether earlier validation (e.g. `TransactionUtil` contract-type checks during `validateSignature`/broadcast) already filters out unrecognized contract types before this code executes.

### Recommendation
Add an explicit null check in `ActuatorFactory.getActuatorByContract()` mirroring `ActuatorCreator`'s behavior — throw a well-defined validation exception (e.g. `ContractValidateException`) when `TransactionFactory.getActuator(contract.getType())` returns `null`, instead of allowing an unchecked `NullPointerException` to propagate.

### Proof of Concept
Not independently verified end-to-end due to tool-call limits; the code-level defect (missing null check before `clazz.newInstance()` in `ActuatorFactory.getActuatorByContract`, contrasted with the guarded `ActuatorCreator` equivalent) is directly confirmed by reading both classes side-by-side as cited above. Full confirmation of exploitability would require tracing which `Wallet.java` API method(s) call `ActuatorFactory.createActuator()` and whether contract-type validation runs before that call.

### Citations

**File:** chainbase/src/main/java/org/tron/core/actuator/TransactionFactory.java (L12-32)
```java
  private static final Map<ContractType, Class<? extends Actuator>> actuatorMap = new ConcurrentHashMap<>();
  private static final Map<ContractType, Class<? extends GeneratedMessageV3>> contractMap = new ConcurrentHashMap<>();

  static {
    register(ContractType.CreateSmartContract, null, CreateSmartContract.class);
    register(ContractType.TriggerSmartContract, null, TriggerSmartContract.class);
  }

  public static void register(ContractType type, Class<? extends Actuator> actuatorClass,
      Class<? extends GeneratedMessageV3> clazz) {
    if (type != null && actuatorClass != null) {
      actuatorMap.put(type, actuatorClass);
    }
    if (type != null && clazz != null) {
      contractMap.put(type, clazz);
    }
  }

  public static Class<? extends Actuator> getActuator(ContractType type) {
    return actuatorMap.get(type);
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ActuatorCreator.java (L60-66)
```java
  private Actuator getActuatorByContract(Contract contract,
      TransactionCapsule tx)
      throws IllegalAccessException, InstantiationException, ContractValidateException {
    Class<? extends Actuator> clazz = TransactionFactory.getActuator(contract.getType());
    if (clazz == null) {
      throw new ContractValidateException("not exist contract " + contract);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ActuatorFactory.java (L37-47)
```java
    rawData.getContractList()
        .forEach(contract -> {
          try {
            actuatorList
                .add(getActuatorByContract(contract, chainBaseManager, transactionCapsule));
          } catch (IllegalAccessException | InstantiationException e) {
            e.printStackTrace();
          }
        });
    return actuatorList;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ActuatorFactory.java (L49-56)
```java
  private static Actuator getActuatorByContract(Contract contract, ChainBaseManager manager,
      TransactionCapsule tx) throws IllegalAccessException, InstantiationException {
    Class<? extends Actuator> clazz = TransactionFactory.getActuator(contract.getType());
    AbstractActuator abstractActuator = (AbstractActuator) clazz.newInstance();
    abstractActuator.setChainBaseManager(manager).setContract(contract)
        .setForkUtils(manager.getForkController()).setTx(tx);
    return abstractActuator;
  }
```
