# **Ephemeral Triad Lifecycle (ETL) Master Strategy**

**Version:** 1.1 (Validated) | **Security Tier:** Zero-Trust Transient Identity

## **1\. Executive Summary**

The ETL objective is to eliminate persistent credentials in GitHub and Kinetic by using a SHA-linked, self-destructing identity model for every CI/CD instantiation. This "Zero-State" model ensures that credentials exist only for the duration of a specific test run and are wiped from all systems immediately upon completion or failure.

## **2\. The Core Identity: The "Transient Triad"**

Each test run is powered by a unique 3-part credential set generated on-demand:

1. **Kinetic URL:** The specific developer environment endpoint.  
2. **X-API-Key:** A GUID-based key created specifically for the current $CommitSHA.  
3. **Bearer Token:** A short-lived JWT scoped strictly to the generated API Key.

## **3\. The Zero-State Lifecycle Sequence**

| Phase | Event | Technical Execution |
| :---- | :---- | :---- |
| **T-Minus 2** | **Identification** | Local script calculates CommitSHA (10-char hex) as the "Baton." |
| **T-Minus 1** | **Provisioning** | Local script fetches Keyring ![][image1] Requests Triad from Kinetic ![][image1] Pushes to GitHub Secrets as TRIAD\_\<SHA\> (using pynacl). |
| **T-Minus 0** | **Hand-off** | git push initiated. **Burn-on-Write:** Local RAM and Env vars are flushed immediately. |
| **T-Plus 1** | **Trigger** | GitHub Runner wakes; maps secrets\["TRIAD\_" \+ GITHUB\_SHA\] to local RAM. |
| **T-Plus 2** | **Destructive Read** | **Path A:** Runner uses PyGithub to DELETE the TRIAD\_\<SHA\> secret from GitHub Cloud. |
| **T-Plus 3** | **Execution** | SDK Integration Tests run using the Triad stored strictly in Runner RAM. |
| **T-Plus 4** | **Teardown** | **Path B/C:** SDK calls Kinetic EF TerminateSession to delete API Key and revoke JWT. |
| **T-Plus 5** | **Case Destruction** | Runner flushes $KINETIC\_TRIAD; VM is decommissioned and wiped. |
| **T-Plus N** | **Scavenging** | **The Executioner:** A process-aware script scans for orphaned/hung TRIAD\_\* secrets. |

## **4\. Detailed Component Specification**

### **A. The "Baton" (Namespace Determinism)**

* **Key ID:** EPICOR\_TRIAD\_{SHA\_PREFIX}  
* **Entropy:** First 10 characters of git rev-parse HEAD.  
* **Collision:** Negligible (![][image2] for 1,000 active commits).

### **B. The Scavenger (Self-Healing Logic)**

The Scavenger is a **State Observer** cron-job with secrets:read and actions:write permissions.

* **Orphan Detection:** If TRIAD\_ABC exists but no GitHub Workflow with head\_sha \== ABC is queued or in\_progress.  
* **Hung Detection:** If a workflow is in\_progress but exceeds the **Stagnation Threshold** (20 mins).  
* **Executioner Mode (No Master Key):** It retrieves the JSON payload of the orphaned secret to authenticate its own "Kill" request to Kinetic. This ensures the Scavenger operates without requiring a persistent high-privilege Master API Key.

### **C. The Kinetic "Suicide" Function (Path B/C)**

The Kinetic Epicor Function (EF) must be non-interactive and use HttpContext reflection:

1. Resolve X-API-Key and Authorization: Bearer from Headers.  
2. Invoke Ice.Lib.IdentityModel.TokenRevocation.Revoke().  
3. Invoke Ice.Contracts.APIKeySvcContract.DeleteByID().  
4. **Validation:** Ensure a deleted Key immediately 401s subsequent calls even with a "valid" token.

## **5\. Failure Mode & Effects Analysis (FMEA)**

| Event | Mitigation | Result |
| :---- | :---- | :---- |
| **Runner Pre-emption** | Scavenger (T+N) detects orphaned secret. | Cleanup within \<30 mins. |
| **Runner Hangs Mid-Test** | Scavenger (T+N) detects stagnation. | Kills session, cancels run. |
| **GitHub API Outage** | Kinetic 8hr TTL Fallback. | Session expires naturally. |
| **Local Script Crash** | Fail before secret push. | No Triad created; Test never starts. |
| **Manual Cancellation** | Scavenger (T+N) detects secret with no process. | Executes wipe. |

## **6\. Implementation Phases**

1. **Phase I: Kinetic Backend:** Deploy SecurityTools EF Library; validate immediate revocation on key deletion.  
2. **Phase II: Local Tooling:** Integrate git-python for SHA and pynacl for secret encryption; implement **Burn-on-Write**.  
3. **Phase III: CI/CD Pipeline:** Configure YAML with if: always() hooks and PyGithub for the **Destructive Read**.  
4. **Phase IV: Observability:** Setup the Scavenger cron-job and logging for "Eradication Events."