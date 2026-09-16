## Analog Found

### Title
Anonymous account permission structure disclosure via `wallet/getsignweight` and `GetTransactionSignWeight` - (File: framework/src/main/java/org/tron/core/services/http/GetTransactionSignWeightServlet.java)

### Summary
The `getTransactionSignWeight` code path, reachable both over the unauthenticated HTTP endpoint `GetTransactionSignWeightServlet` and the gRPC `Wallet` service, accepts an arbitrary (optionally unsigned) transaction whose `owner_address` and `Permission_id` reference *any* account on the chain, and returns that account's complete `Permission` object — including every key address, per-key weight, threshold, and the 32-byte `operations` bitmap describing which contract types the permission may execute — without requiring the caller to hold any of those keys or provide valid matching signatures.

### Finding Description
`TransactionUtil.getTransactionSignWeight()` looks up the `owner_address` account, resolves the requested `permissionId` via `AccountCapsule.getPermissionById`, and unconditionally attaches the full `Permission` to the response with `tswBuilder.setPermission(permission)` [1](#0-0) . This happens before any real signature-weight requirement is enforced — the method only checks operation-type membership via `checkPermissionOperations`, and it computes `checkWeight` only if `trx.getSignatureCount() > 0`; a request with zero signatures still returns the permission data with `NOT_ENOUGH_PERMISSION` [2](#0-1) .

The HTTP servlet `GetTransactionSignWeightServlet.doPost` builds the transaction directly from unauthenticated POST params and forwards it straight to `transactionUtil.getTransactionSignWeight(transaction)`, printing the full reply including the `Permission` field [3](#0-2) . The equivalent gRPC RPC exposes the same call. No caller identity, ownership, or key possession is verified before the multi-sig configuration of an arbitrary account is disclosed — the caller only needs to know (or guess) the target `owner_address`, put it into an arbitrary (even semantically invalid) contract, and submit it as `params`/`visible` JSON.

This mirrors the referenced Backstage advisory's bug class (CWE-213): a component designed to answer an authorization/weight-sufficiency query about a *specific caller-supplied* credential set instead leaks the entirety of the underlying policy/permission object (here: all authorized signer addresses, their weights, threshold, and allowed-operation bitmap) to whoever asks, regardless of whether they hold any authorization over the target account.

### Impact Explanation
An anonymous, unprivileged client can enumerate the full multi-signature governance structure of any TRON account (owner/witness/active permissions) — the set of controlling addresses, their relative signing weights, the approval threshold, and precisely which contract types each active permission is scoped to. This is sensitive operational-security information: it lets an attacker map out exactly which keys/addresses must be compromised (and how many, with what weights) to seize control of a specific account's assets or governance actions, materially aiding targeted key-compromise or social-engineering campaigns against multi-sig-protected accounts (e.g. exchange hot wallets, DAO treasuries, SR/witness accounts). This matches CWE-213/Medium confidentiality-only severity (no direct fund movement), consistent with the original advisory's CVSS `C:L`.

### Likelihood Explanation
Trivial to exploit: the endpoint is unauthenticated, rate-limited only generically (`RateLimiterServlet`), and requires no valid signature — an attacker just needs the target account's base58 address and submits a POST to `wallet/getsignweight` (or the gRPC equivalent) with `permission_id` values 0/1/2/etc. to enumerate owner/witness/active permissions. This is directly reachable by any anonymous API client per the in-scope threat model.

### Recommendation
Do not populate/return the raw `Permission` object (keys, weights, threshold, operations) for accounts/permissions the caller cannot demonstrate any signing authority over. At minimum, strip or redact key-level detail (addresses/weights) from `TransactionSignWeight` responses when the submitted transaction carries zero or insufficient matching signatures, returning only the minimal information needed (e.g., threshold-met boolean) unless the caller has already supplied at least one valid signature belonging to that permission's key set.

### Proof of Concept
1. Identify any account address `A` known (or suspected) to use multi-sig permissions.
2. Construct a JSON transaction body referencing `owner_address = A` and `Permission_id = 2` (active) with any contract type, and zero signatures.
3. POST it to `http://<node>:8090/wallet/getsignweight`.
4. Observe the response contains the full `permission` field: all `keys` (addresses + weights), `threshold`, and `operations` bitmap for account `A`, despite the caller providing no valid signature or authorization for that account. [4](#0-3) [5](#0-4)

### Citations

**File:** actuator/src/main/java/org/tron/core/utils/TransactionUtil.java (L199-258)
```java
  public TransactionSignWeight getTransactionSignWeight(Transaction trx) {
    TransactionSignWeight.Builder tswBuilder = TransactionSignWeight.newBuilder();
    Result.Builder resultBuilder = Result.newBuilder();
    if (trx.getSignatureCount() > chainBaseManager.getDynamicPropertiesStore()
        .getTotalSignNum()) {
      resultBuilder.setCode(Result.response_code.OTHER_ERROR);
      resultBuilder.setMessage("too many signatures");
      tswBuilder.setResult(resultBuilder);
      return tswBuilder.build();
    }

    trx = truncateSignatures(trx);
    TransactionExtention.Builder trxExBuilder = TransactionExtention.newBuilder();
    trxExBuilder.setTransaction(trx);
    trxExBuilder.setTxid(ByteString.copyFrom(Sha256Hash.hash(CommonParameter
        .getInstance().isECKeyCryptoEngine(), trx.getRawData().toByteArray())));
    Return.Builder retBuilder = Return.newBuilder();
    retBuilder.setResult(true).setCode(response_code.SUCCESS);
    trxExBuilder.setResult(retBuilder);
    tswBuilder.setTransaction(trxExBuilder);

    if (trx.getRawData().getContractCount() == 0) {
      resultBuilder.setCode(Result.response_code.OTHER_ERROR);
      resultBuilder.setMessage("Invalid transaction: no valid contract");
    } else {
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
        if (permissionId != 0) {
          if (permission.getType() != PermissionType.Active) {
            throw new PermissionException("Permission type is wrong!");
          }
          //check operations
          if (!checkPermissionOperations(permission, contract)) {
            throw new PermissionException("Permission denied!");
          }
        }
        tswBuilder.setPermission(permission);
        if (trx.getSignatureCount() > 0) {
          List<ByteString> approveList = new ArrayList<>();
          long currentWeight = TransactionCapsule.checkWeight(permission, trx.getSignatureList(),
              Sha256Hash.hash(CommonParameter.getInstance()
                  .isECKeyCryptoEngine(), trx.getRawData().toByteArray()), approveList);
          tswBuilder.addAllApprovedList(approveList);
          tswBuilder.setCurrentWeight(currentWeight);
        }
        if (tswBuilder.getCurrentWeight() >= permission.getThreshold()) {
          resultBuilder.setCode(Result.response_code.ENOUGH_PERMISSION);
        } else {
          resultBuilder.setCode(Result.response_code.NOT_ENOUGH_PERMISSION);
        }
```

**File:** framework/src/main/java/org/tron/core/services/http/GetTransactionSignWeightServlet.java (L1-38)
```java
package org.tron.core.services.http;

import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;
import org.tron.api.GrpcAPI.TransactionSignWeight;
import org.tron.core.utils.TransactionUtil;
import org.tron.protos.Protocol.Transaction;


@Component
@Slf4j(topic = "API")
public class GetTransactionSignWeightServlet extends RateLimiterServlet {

  @Autowired
  private TransactionUtil transactionUtil;

  protected void doGet(HttpServletRequest request, HttpServletResponse response) {

  }

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
}
```
