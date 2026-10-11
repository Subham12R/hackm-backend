# HackMatrix domain context

## Canonical terms

- **Event:** one HackMatrix edition with its own dates, rules, pricing, challenge, and result lifecycle.
- **Registration:** a team's request to enter an event, including consent, submitted member data, quote, payment state, and review state.
- **Team:** the operational group admins review and the participant leader owns after registration.
- **Member:** a person in a team's roster; only the leader requires an account in the first release.
- **User:** an authenticated identity. A user may hold participant, admin, judge, or viewer capabilities.
- **Activation:** the one-time password setup that follows a verified paid leader registration.
- **Payment order:** the server-created provider order for one immutable registration quote.
- **Payment attempt:** a provider payment attached to an order; a browser success callback is not proof of capture.
- **Submission:** a team's project draft or final snapshot, owned by the team and visible through role-specific projections.
- **Evaluation:** one judge's rubric response for one assigned anonymous submission.
- **Result release:** the explicit audited action that makes a locked result snapshot public.
- **Event completion:** an organiser's audited, reversible mark that a stage (shown as an "Event") is over; its certificates can only be published after it.
- **Certificate type:** one kind of certificate for a stage (Participation, Winner, ...): blank artwork plus the placed fields the API draws on it.
- **Badge:** a digital badge defined once (image, description, criteria) and awarded to many members; an award may carry its own image.
- **Credential:** one certificate or badge held by one team member, with a public code (`HMX-XXXX-XXXX`) and verify page. DRAFT, then PUBLISHED, then optionally REVOKED.

## Invariants

- PostgreSQL is the source of truth; frontend fixtures and browser storage are previews only.
- A registration quote is calculated from server-side rules and cannot be replaced by client input.
- A participant account cannot log in before activation; activation requires the registration's verified payment policy.
- Passwords use Argon2id; password hashes are never logged or returned.
- Payment confirmation requires provider verification and durable webhook/idempotency handling.
- Participants can read and mutate only their own team and permitted event records.
- Judges receive anonymous submission projections and never receive identity mappings through the API.
- Final submissions, submitted evaluations, locked results, and audit events are append-only or require an explicit audited reopen path.
- A public leaderboard exists only after an authorized release action.
- Participants and the public never see a draft credential; certificates publish only for a completed stage, badges only need publishing.
- A credential's code never changes: corrections redraw it as a new version, and a revoked credential stays verifiable as revoked.
