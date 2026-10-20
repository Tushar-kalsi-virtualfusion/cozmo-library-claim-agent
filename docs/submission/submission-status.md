# Submission status - 20 October 2026

This release contains the as-built hybrid iPhone/server prototype, two supplied
screen recordings, a partial real-device claim packet with traceable evidence,
a results workbook, a one-page architecture note, a detailed technical report and
a one-page failure log. It is a partial assessment submission, not a claim that
all acceptance bars passed.

## Review order

1. [README](../../README.md): setup and run instructions.
2. [Architecture note](../../output/pdf/architecture-note.pdf) and [full technical report](../../output/pdf/technical-report.pdf): model boundaries, voice/video coordination, LiDAR scale, identity, pricing and persistence.
3. [Original recordings and checksums](videos.md), available from the [release](https://github.com/Tushar-kalsi-virtualfusion/cozmo-library-claim-agent/releases/tag/assessment-2026-10-20).
4. [Claim evidence ZIP](../../submission/evidence/current-evidence.zip): extract it and open `current/report.html`; JSON and all referenced evidence stay together.
5. [Results workbook](../../submission/ground-truth-and-results.xlsx), [results notes](results.md), [failure log](../../output/pdf/failure-log.pdf) and [software verification](validation.md).

## What the evidence establishes

The saved sweep contains 3 book records and 76 non-book records after a
conservative post-capture identity repair. Those are system predictions, not
verified physical counts. The bundle preserves the saved values and referenced
media, and distinguishes later repairs from what occurred during the original
capture. Source-linked prices remain estimates with explicit identity caveats.
The Python suite, selected Swift checks, clean dependency installation and an
unsigned iPhone build passed. These checks do not establish sensor accuracy.

## Remaining assessment gaps

- The required 60-book, two-shelving-unit scenario is not demonstrated.
- Independent counts, title checks, 20 measured spines, 15 checked prices and tape-measured room dimensions were not supplied. The workbook leaves those cells blank and pass bars unverified.
- The main 2:50 capture has no audio track and ends during capture. The separate 47-second recording shows later inventory. Together they do not satisfy the required single continuous audible capture-through-packet demonstration.
- The current packet is partial. Floor area, room length/width and finish/packet-ready timestamps are absent. No measured five-minute completion claim is made.
- Further provider pricing was blocked by `credit_balance_exhausted`. Known API cost is a partial estimate, not a complete invoice.

## Delivery and public scope

The assessment asked for a private repository. The owner explicitly requested a
public repository for this handoff. The source snapshot excludes local keys,
pairing configuration, private development history, the original email/brief copy,
raw conversation transcripts and unrelated diagnostic captures. Selected claim
evidence and both owner-supplied recordings are intentional submission assets.
Model provenance and third-party notices are retained.

The original requested deadline was 19 October 2026 at 20:00 IST. This release is
dated 20 October 2026 and is a late submission. The brief stated that late work
would be reviewed with the delay noted. Repository publication is separate from
email delivery; the release alone does not prove the reviewer was notified.
