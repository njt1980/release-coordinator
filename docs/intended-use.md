# Intended use and claims

Approved 2026-11-14. Every user-facing claim about the platform must agree with this page.

## Intended use
Release Coordinator helps a person decide whether, and in which order, to release a change to a
microservices application. For one pull request it gathers evidence (source code, Kubernetes
manifests, telemetry, the project's dependency review), shows which services the change can affect
and a deployment order, and lists what the evidence cannot show. A named person makes and records
the release decision.

## It is
- Decision support for release and change control.
- A record of the evidence an assessment used, and of the person's decision on it.

## It is not
- Not a medical device, and not part of one; it does not process patient data for diagnosis or
  treatment.
- Not validated software. The customer validates it for its own intended use in its quality system.
- Not "compliant" with 21 CFR Part 11, IEC 62304, ISO 13485 or the MDR. The decision record is
  designed to support those requirements (signature manifestation, audit trail, record linking);
  whether it meets them is established by the customer's validation.
- Not an approver. It never releases or blocks a deployment. "Go (advisory)" means every automated
  check passed and the advisory review agreed; the decision is the person's.
- Not a complete impact analysis. The blast radius covers dependencies the evidence shows; the
  briefing lists what it could not see (unscanned files, unobserved services, unresolved addresses).
- Not a risk assessment under ISO 14971. The advisory risk review is AI-generated text that
  supports the person's judgement.

## Where each part of the output comes from (field labels)
| Label | Meaning | Examples |
|---|---|---|
| Computed | Derived by deterministic code from recorded evidence; reproducible from the same inputs | blast radius, deployment order, reason codes, coverage, observability gaps |
| Human | Entered by a named person | review answers, release decision, reason, regulated-service list, policy |
| AI-generated | Written by the external model; may be wrong | advisory risk score and justification, assistant answers |

AI-generated text is never part of the signed decision record. The record stores the hash of the
assessment shown, so what the person saw can be checked later. If the person relies on the AI text,
they say so in their own words in the decision's reason.
