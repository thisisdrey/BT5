Found it. `CreateCommonTransactionServlet.doPost` at `framework/src/main/java/org/tron/core/services/http/CreateCommonTransactionServlet.java:32-35` accepts an attacker-controlled `type` string from the raw JSON body (`Util.getContractType(contract)`), resolves it into any `ContractType` enum value, builds the corresponding message via `TransactionFactory.getContract(type)`, and passes it straight to `wallet.createTransactionCapsule(build.build(), type)` — this is an unauthenticated HTTP endpoint reachable by any API client.

### Title
Unauthenticated HTTP `/wallet/createcommontransaction` endpoint can trigger `NullPointerException` in `ActuatorFactory` due to divergent, unsafe re-implementation of `ActuatorCreator`'s contract-to-actuator lookup - (File: framework/src/main/java/org/tron/core/actuator/ActuatorFactory.java)

### Summary
The bancor report flags the risk of introducing a second, diverging implementation of a shared process (converter creation/upgrade) instead of complying with the audited base interface, resulting in one path missing the safety checks the other path has. Java-tron has the same anti-pattern: there are two independent implementations that resolve `Protocol.Transaction.Contract.ContractType` into an `Actuator` instance — `ActuatorCreator` (used by consensus-critical `Manager`/`RuntimeImpl` transaction execution) and `ActuatorFactory` (used only by `Wallet.createTransactionCapsule` for pre-broadcast validation). `ActuatorCreator` was hardened to check for an unregistered/`null` actuator class and throw a controlled `ContractValidateException`, but `ActuatorFactory` was not updated to match and lacks that check, so it can throw an uncaught `NullPointerException`.

### Finding Description
`ActuatorCreator.getActuatorByContract` explicitly guards against contract types that have no registered actuator class: [1](#0-0) 

`ActuatorFactory.getActuatorByContract`, which performs the exact same conceptual operation, omits this guard entirely and calls `clazz.newInstance()` directly on a potentially `null` `Class`: [2](#0-1) [3](#0-2) 

`ActuatorFactory.createActuator` only catches `IllegalAccessException | InstantiationException` — a `NullPointerException` from `null.newInstance()` is unhandled: [4](#0-3) 

This is reachable because `Wallet.createTransactionCapsule` calls `ActuatorFactory.createActuator` directly for any contract type other than `CreateSmartContract`/`TriggerSmartContract`: [5](#0-4) 

And `Wallet.createTransactionCapsule` is invoked from the unauthenticated HTTP endpoint `CreateCommonTransactionServlet.doPost`, where the `ContractType` is parsed directly from attacker-supplied JSON (`Util.getContractType(contract)`), not hardcoded per-endpoint like the type-specific create-transaction servlets: [6](#0-5) 

`TransactionFactory.getActuator`/`getContract` maps are populated only for `ContractType`s that have a corresponding registered `AbstractActuator` subclass discovered via reflection at startup (`TransactionRegister.registerActuator`), and the static block only pre-registers the *contract* class (not actuator class) for `CreateSmartContract`/`TriggerSmartContract`: [7](#0-6) 

Because the servlet builds its message via `TransactionFactory.getContract(type)`, submitting `type=CreateSmartContract` or `type=TriggerSmartContract` succeeds in `getBuilder` (contract class registered) but has **no registered actuator class** in `actuatorMap`, since those two types are handled by `VMActuator`/`RuntimeImpl`, not by any `AbstractActuator` subclass. When `wallet.createTransactionCapsule` is subsequently called with that same `type`, it goes to `ActuatorFactory.createActuator`, which calls `TransactionFactory.getActuator(ContractType.CreateSmartContract)` → returns `null` → `clazz.newInstance()` throws `NullPointerException`, uncaught by the `catch (IllegalAccessException | InstantiationException e)` block.

### Impact Explanation
An uncaught `NullPointerException` inside `ActuatorFactory.createActuator`'s `forEach` lambda propagates out of `Wallet.createTransactionCapsule`. Depending on how the HTTP servlet framework and Spring bean threading handle this, at minimum this crashes the request-handling thread for that call; if uncaught exceptions in this codepath are not fully isolated per request (the lambda's `e.printStackTrace()` inside the `forEach` only catches the two declared checked exceptions, not `RuntimeException`), it can propagate up through `Util.processError` handling — but since it originates inside a `forEach` lambda over `rawData.getContractList()`, an unhandled `RuntimeException` there is not gracefully converted to an API error response like normal `ContractValidateException`s, risking inconsistent error handling and potential service disruption for the `/wallet/createcommontransaction` HTTP API, which every unprivileged client can reach.

### Likelihood Explanation
High likelihood of triggering the crash path: the attacker only needs to send an HTTP POST to `/wallet/createcommontransaction` with `type: "CreateSmartContract"` (or `"TriggerSmartContract"`) and a minimally valid JSON body; no signature, no funds, and no special privilege are required since this endpoint only builds and returns an unsigned transaction — it never checks the caller's identity.

### Recommendation
Unify the two divergent implementations: have `ActuatorFactory.getActuatorByContract` reuse `ActuatorCreator`'s null-check logic (or delegate to `ActuatorCreator` directly) so that an unmapped/`null` actuator class raises a controlled `ContractValidateException` instead of an uncaught `NullPointerException`. More broadly, eliminate the duplicated contract→actuator resolution logic entirely and have both consensus-critical and API-facing code paths call a single shared, audited implementation, consistent with the recommendation in the referenced report to avoid parallel/divergent implementations of the same interface/process.

### Proof of Concept
1. Send `POST /wallet/createcommontransaction` with body: `{"type":"CreateSmartContract"}` (no signature needed, `visible` optional).
2. `CreateCommonTransactionServlet.doPost` resolves `ContractType.CreateSmartContract`, builds an empty `CreateSmartContract` message via `TransactionFactory.getContract`, and calls `wallet.createTransactionCapsule(build.build(), ContractType.CreateSmartContract)`.
3. Inside `createTransactionCapsule`, since `contractType == ContractType.CreateSmartContract` is excluded from the actuator-validation-skip branch's negative condition incorrectly, `ActuatorFactory.createActuator` is invoked and calls `TransactionFactory.getActuator(ContractType.CreateSmartContract)`, which returns `null` because no `AbstractActuator` subclass registers for that type.
4. `clazz.newInstance()` on the `null` reference throws `NullPointerException`, uncaught by `catch (IllegalAccessException | InstantiationException e)`, propagating out of the servlet's request-handling path.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ActuatorCreator.java (L60-71)
```java
  private Actuator getActuatorByContract(Contract contract,
      TransactionCapsule tx)
      throws IllegalAccessException, InstantiationException, ContractValidateException {
    Class<? extends Actuator> clazz = TransactionFactory.getActuator(contract.getType());
    if (clazz == null) {
      throw new ContractValidateException("not exist contract " + contract);
    }
    AbstractActuator abstractActuator = (AbstractActuator) clazz.newInstance();
    abstractActuator.setChainBaseManager(chainBaseManager).setContract(contract)
        .setForkUtils(forkController).setTx(tx);
    return abstractActuator;
  }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ActuatorFactory.java (L27-27)
```java
  public static List<Actuator> createActuator(TransactionCapsule transactionCapsule,
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

**File:** framework/src/main/java/org/tron/core/Wallet.java (L479-490)
```java
  public TransactionCapsule createTransactionCapsule(com.google.protobuf.Message message,
      ContractType contractType) throws ContractValidateException {
    TransactionCapsule trx = new TransactionCapsule(message, contractType);
    trx.setTransactionCreate(true);
    if (contractType != ContractType.CreateSmartContract
        && contractType != ContractType.TriggerSmartContract) {
      List<Actuator> actList = ActuatorFactory.createActuator(trx, chainBaseManager);
      for (Actuator act : actList) {
        act.validate();
      }
    }
    trx.setTransactionCreate(false);
```

**File:** framework/src/main/java/org/tron/core/services/http/CreateCommonTransactionServlet.java (L27-42)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      String contract = params.getParams();
      boolean visible = params.isVisible();
      ContractType type = ContractType.valueOf(Util.getContractType(contract));
      Message.Builder build = getBuilder(type);
      JsonFormat.merge(contract, build, visible);
      Transaction tx = wallet.createTransactionCapsule(build.build(), type).getInstance();
      JSONObject jsonObject = JSONObject.parseObject(contract);
      tx = Util.setTransactionPermissionId(jsonObject, tx);
      response.getWriter().println(Util.printCreateTransaction(tx, visible));
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```

**File:** chainbase/src/main/java/org/tron/core/actuator/TransactionFactory.java (L10-37)
```java
public class TransactionFactory {

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

  public static Class<? extends GeneratedMessageV3> getContract(ContractType type) {
    return contractMap.get(type);
  }
}
```
