### Title
Unauthenticated `/wallet/getsignweight` and `/wallet/getapprovedlist` endpoints leak account existence via response content/branching - ([File: actuator/src/main/java/org/tron/core/utils/TransactionUtil.java])

### Summary
`GetTransactionSignWeightServlet` and `GetTransactionApprovedListServlet` are unauthenticated, anonymously reachable HTTP endpoints that accept an arbitrary (even unsigned) transaction body and directly return whether the `owner_address` in the transaction is an existing on-chain account, distinguishing this from other failure modes such as bad permission or bad signature. This is an account-enumeration oracle, analogous in effect to the Django `ModelBackend.authenticate()` issue where account existence could be inferred from backend behavior differences.

### Finding Description
`TransactionUtil.getTransactionSignWeight()` inspects the first contract of a submitted transaction, extracts the `owner` address, and looks it up: [1](#0-0) 

If the account does not exist, it throws `PermissionException("Account does not exist!")`, which is caught and surfaced verbatim in the HTTP/gRPC response as `PERMISSION_ERROR` with that exact message: [2](#0-1) 

If the account does exist but the permission id is invalid, a different message ("Permission for this, does not exist!") is returned; if the account and permission both exist, the code proceeds to full signature-weight computation and returns `ENOUGH_PERMISSION`/`NOT_ENOUGH_PERMISSION`. The exact same account-existence branch and distinct-message pattern exists in `Wallet.getTransactionApprovedList()`: [3](#0-2) 

Both are wired to unauthenticated HTTP servlets that accept a raw `Transaction` protobuf from the POST body with no signature or authorization required — the servlet simply packs the request and forwards it: [4](#0-3) 

An anonymous client can construct a transaction whose `owner_address` field is set to an arbitrary address of interest, with zero or malformed signatures, and observe the differing response messages/response codes (`Account does not exist!` vs `Permission for this, does not exist!` vs `NOT_ENOUGH_PERMISSION`) to determine, oracle-style, whether that address is an activated account on-chain — without needing any valid signature, private key, or on-chain interaction from the target address.

### Impact Explanation
This is a direct information-disclosure oracle for account existence, reachable by any anonymous HTTP/gRPC client with no rate-limiting beyond the generic servlet limiter and no cost (no fee, no valid signature, no broadcast required). It enables mass enumeration/probing of which addresses are activated accounts on the TRON chain, which can be leveraged for targeted phishing, deanonymization of wallet activity, or reconnaissance ahead of further attacks (e.g., correlating off-chain identity with on-chain account activation). It does not itself allow theft of funds, but it is a concrete, unauthorized information leak analogous in class to the CVE referenced (user enumeration via observable authentication-path behavior).

### Likelihood Explanation
High likelihood of exploitation: the endpoint is intentionally public (used by wallets to check multisig weight before broadcast), requires no authentication, and the differing messages are returned unconditionally for any malformed/unsigned transaction. An attacker only needs to submit a transaction JSON with an arbitrary `owner_address` and no valid signature list.

### Recommendation
Normalize the response for "account does not exist" and "permission does not exist" (and other pre-signature-check failures) into a single generic error/response code so account existence cannot be distinguished from permission or signature validity failures. Consider also requiring the request to include a properly-formed signature list of nonzero length before performing account/permission lookups, and rate-limit this endpoint more aggressively.

### Proof of Concept
1. Send `POST /wallet/getsignweight` (or `/wallet/getapprovedlist`) with a JSON body containing `raw_data.contract[0].parameter.value.owner_address` set to an address `A`, and no `signature` entries.
2. If `A` is not an activated account, the response contains `code: PERMISSION_ERROR`, `message: "Account does not exist!"` — from [5](#0-4) .
3. If `A` is an activated account, the response instead proceeds to permission/weight evaluation and returns a different code/message (e.g. `NOT_ENOUGH_PERMISSION` or a permission-specific error).
4. Repeating this for a list of candidate addresses lets an anonymous caller enumerate which addresses are activated accounts on-chain, with no signature or fee required.

### Citations

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L224-235)
```java
      try {
        Contract contract = trx.getRawData().getContract(0);
        byte[] owner = TransactionCapsule.getOwner(contract);
        AccountCapsule account = chainBaseManager.getAccountStore().get(owner);
        if (Objects.isNull(account)) {
          throw new PermissionException("Account does not exist!");
        }
        int permissionId = contract.getPermissionId();
        Permission permission = account.getPermissionById(permissionId);
        if (permission == null) {
          throw new PermissionException("Permission for this, does not exist!");
        }
```

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L265-271)
```java
      } catch (PermissionException permEx) {
        resultBuilder.setCode(Result.response_code.PERMISSION_ERROR);
        resultBuilder.setMessage(permEx.getMessage());
      } catch (Exception ex) {
        resultBuilder.setCode(Result.response_code.OTHER_ERROR);
        resultBuilder.setMessage(ex.getClass() + " : " + ex.getMessage());
      }
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L660-669)
```java
        byte[] owner = TransactionCapsule.getOwner(contract);
        AccountCapsule account = chainBaseManager.getAccountStore().get(owner);
        if (account == null) {
          throw new PermissionException("Account does not exist!");
        }
        int permissionId = contract.getPermissionId();
        Permission permission = account.getPermissionById(permissionId);
        if (permission == null) {
          throw new PermissionException("Permission for this, does not exist!");
        }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetTransactionSignWeightServlet.java (L24-37)
```java
  protected void doPost(HttpServletRequest request, HttpServletResponse response) {
    try {
      PostParams params = PostParams.getPostParams(request);
      Transaction transaction = Util.packTransaction(params.getParams(), params.isVisible());
      TransactionSignWeight reply = transactionUtil.getTransactionSignWeight(transaction);
      if (reply != null) {
        response.getWriter().println(Util.printTransactionSignWeight(reply, params.isVisible()));
      } else {
        response.getWriter().println("{}");
      }
    } catch (Exception e) {
      Util.processError(e, response);
    }
  }
```
