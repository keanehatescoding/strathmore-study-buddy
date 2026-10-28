# Notification drafts — incident 2026-10-23

Two drafts: an email to affected users and a breach notification to the
ODPC. Fill every `[bracket]`, delete any sentence you cannot stand behind,
and have someone who is not the incident lead read it before it goes out.

**Send from a channel that was not exposed.** If Resend was in the leaked
environment, do not use it for the notice: send through the university's
mail or another provider. Record who sent what, to how many people, and when
in `docs/incident-2026-10-23.md`.

---

## 1. Email to affected users

**Subject:** Important: security incident affecting Study Buddy

Hello,

We are writing to tell you about a security incident affecting Study Buddy
(`[app host]`), the study app that syncs your Moodle and Google Classroom
material.

**What happened.** On `[date]`, we found that the hosting account that runs
Study Buddy had been accessed without authorisation. `[One sentence on the
confirmed cause, e.g. "An attacker used a stolen deployment token" — omit if
still unknown.]` We took the service offline, changed every credential, and
rebuilt it from clean code.

**What may have been affected.** The account could have reached the data
Study Buddy holds: your name and Strathmore email address, your synced course
material, assignments and quiz/review history, and the access tokens that let
Study Buddy read your Moodle and Google Classroom/Drive content. We do not
store your Moodle password, and Study Buddy never receives your Google
password.

**What we know so far.** `[Choose one: "We have found no evidence that your
data was actually accessed." / "We have evidence that data was accessed." /
"We cannot yet rule out that data was accessed."]` We are continuing to
investigate and will update you if that changes.

**What we have done.**

- Signed every user out and invalidated all access tokens held by the app.
- Replaced the key that protected stored Moodle and Google tokens; the old
  copies can no longer be used by us.
- Asked Google to revoke Study Buddy's access, and asked Moodle to invalidate
  the affected tokens.
- Rebuilt and redeployed from a known-good version of the code.

**What you should do.**

1. Sign in again at `[app URL]` and reconnect Moodle under Settings. You will
   be asked to approve Google access again — that is expected.
2. Review recent activity on your Google account at
   `myaccount.google.com/security-checkup`, and check
   `myaccount.google.com/permissions` for anything you do not recognise.
3. Be alert to phishing emails that mention Study Buddy, your courses, or your
   Strathmore account. We will never ask for your password by email.
4. If you reused your Moodle password anywhere else, change it there too.

We are sorry this happened. If you have questions, contact `[name]` at
`[email]` / `[phone]`.

`[Name]`
`[Role]`, Study Buddy
`[Date]`

---

## 2. Notification to the Office of the Data Protection Commissioner

> Kenya's Data Protection Act 2019, s.43: notify the ODPC within 72 hours of
> becoming aware of a breach that is likely to result in a risk to data
> subjects, with the nature of the breach, categories and approximate numbers
> involved, contact details, likely consequences, and measures taken. s.44:
> also inform data subjects where the breach is likely to result in high risk
> to their rights and freedoms. Verify the current ODPC submission channel and
> required form before sending.

**Data controller:** `[legal name]`, `[postal address]`
**Contact / data protection focal point:** `[name, title, email, phone]`
**Date of notification:** `[date]`
**Date the controller became aware:** `[date/time]`

### Nature of the breach

Unauthorised access to the third-party hosting account (Railway) used to run
the Study Buddy application, which provides `[one-line description]` for
students at Strathmore University. The access `[was/was not/may have been]`
used to reach the application's environment configuration and its PostgreSQL
database. `[State any confirmed deployment of unauthorised code.]`

### Categories and approximate number of data subjects

| Category | Approximate number |
|---|---|
| Students with an account | `[N]` |
| Records: names and institutional email addresses | `[N]` |
| Records: synced course material, assignments, quiz/review history | `[N]` |
| Records: Moodle access tokens (encrypted at rest) | `[N]` |
| Records: Google OAuth refresh tokens granting Classroom/Drive read access | `[N]` |

No passwords, financial data, national ID numbers, health data or biometric
data are stored by the application.

### Likely consequences

`[Choose and justify: unauthorised access to academic records and course
material; impersonation of the application to users; access to students'
Classroom/Drive content through the tokens; phishing using the service's
sending domain. If access is not confirmed: "At the time of notification we
have not established that personal data was accessed; notification is
precautionary."]`

### Measures taken

1. `[Contained the hosting account: password and MFA changed, all API tokens
   and sessions revoked, unauthorised services and deployments removed.]`
2. Signed out all users and invalidated all sessions.
3. Revoked the Google OAuth client and all issued refresh tokens; students
   re-consent on next sign-in.
4. Invalidated all Moodle web-service tokens at the Moodle site.
5. Rotated the database password, the application signing/encryption key,
   the email provider key and the LLM provider key.
6. Rebuilt and redeployed from a verified clean commit.
7. Notified affected data subjects on `[date]` via `[channel]`.
8. `[Opened an internal review; actions: ...]`

### Attachments

- `[Evidence index / DB audit summary, with secrets and personal data redacted]`
- `[Copy of the user notice]`

---

## 3. Internal note for support replies

- Do not speculate about who was responsible or what they read. Use the same
  "what we know" sentence as the user email.
- Direct account-specific questions to `[email]`; log them for the record.
- Never ask a user for their Moodle password or a Google verification code.
- If a user reports unusual Google/Moodle activity, record it as a possible
  indicator and escalate to the incident lead the same day.
