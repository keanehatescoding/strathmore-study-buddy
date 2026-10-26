"""Admin chores: python -m app.admin_cli {claim-unowned,revoke-sessions,lockdown,delete-user} EMAIL

claim-unowned: courses synced before sign-in existed have no owner and are
hidden from everyone. They are claimed automatically when MOODLE_TOKEN_OWNER
signs in; this assigns them by hand.

revoke-sessions: signs a user out everywhere, e.g. after a lost device.
(Removing an address from ALLOWED_EMAILS already ends its sessions.)

lockdown: incident response for a leaked SECRET_KEY or database (see
docs/incident-response.md). Signs *every* user out, revokes each stored
Google grant with Google and forgets every stored Google/Moodle token.
Run it before rotating SECRET_KEY, or the stored tokens no longer decrypt
and their grants have to be killed by deleting the OAuth client instead.
Whatever it could not prove dead (a Google grant it could not revoke, any
Moodle token: Moodle's API can't revoke them) is recorded, so lockdown keeps
exiting 1 on every retry until you remove the grants yourself (delete the
OAuth client / the tokens in Moodle) and pass --google-revoked /
--moodle-revoked. MOODLE_TOKEN in the environment counts until you pass
--moodle-revoked or unset it.

delete-user: what Settings -> Delete account does, for a user who asks you
to: revokes their Google grant and deletes them with all their data. Take
the address off ALLOWED_EMAILS as well, or signing in starts a new account.
"""

from __future__ import annotations

import argparse

from sqlmodel import Session

from app.account import SyncRunning, delete_account
from app.auth import claim_unowned, find_user, lock_down, revoke_sessions
from app.db import engine


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    claim = sub.add_parser("claim-unowned", help="assign all unowned courses to a user")
    claim.add_argument("email")
    revoke = sub.add_parser("revoke-sessions", help="sign a user out on every device")
    revoke.add_argument("email")
    lock = sub.add_parser(
        "lockdown",
        help="sign everyone out and forget every stored Google/Moodle token "
             "(incident response; run before rotating SECRET_KEY)",
    )
    lock.add_argument("--dry-run", action="store_true",
                      help="report what would change, without changing it")
    lock.add_argument("--no-google-revoke", action="store_true",
                      help="clear stored Google tokens without asking Google to revoke")
    lock.add_argument("--keep-moodle", action="store_true",
                      help="leave stored Moodle tokens in place")
    lock.add_argument("--google-revoked", action="store_true",
                      help="confirm the Google grants that could not be revoked here "
                           "were removed at Google (OAuth client deleted or grants "
                           "revoked); clears their records")
    lock.add_argument("--moodle-revoked", action="store_true",
                      help="confirm every Moodle token (stored, forgotten and "
                           "MOODLE_TOKEN) was deleted in Moodle; clears their records")
    remove = sub.add_parser("delete-user", help="delete a user and all their data")
    remove.add_argument("email")
    args = parser.parse_args()

    with Session(engine) as session:
        if args.command == "lockdown":
            report = lock_down(
                session,
                revoke_google=not args.no_google_revoke,
                keep_moodle=args.keep_moodle,
                dry_run=args.dry_run,
                ack_google=args.google_revoked,
                ack_moodle=args.moodle_revoked,
            )
            if args.dry_run:
                print(f"dry run: would sign out every user and forget credentials "
                      f"for {report.cleared} of them")
            else:
                print(f"signed out every user; forgot credentials for {report.cleared}")
                print(f"google grants revoked: {report.google_revoked}"
                      + (f", could not revoke: {report.google_failed}"
                         if report.google_failed else "")
                      + (f", left alone: {report.google_skipped}"
                         if report.google_skipped else ""))
            if report.undecryptable:
                print(f"{report.undecryptable} stored Google token(s) did not decrypt "
                      "(SECRET_KEY already rotated?); delete the OAuth client at Google "
                      "to kill the grants behind them")
            if report.google_skipped:
                print(f"{report.google_skipped} stored Google token(s) were not revoked "
                      "(--no-google-revoke); revoke them or delete the OAuth client at "
                      "Google")
            if report.shared_google:
                # --no-google-revoke wins over --dry-run: no revocation is attempted
                if args.no_google_revoke:
                    print("GOOGLE_REFRESH_TOKEN is configured as a shared fallback and was "
                          "not revoked (--no-google-revoke); revoke it or delete the "
                          "OAuth client, then replace or remove it")
                elif args.dry_run:
                    print("GOOGLE_REFRESH_TOKEN is configured as a shared fallback; dry "
                          "run: would revoke it with Google, then replace or remove it")
                elif report.shared_google_revoked:
                    print("revoked the shared GOOGLE_REFRESH_TOKEN with Google; replace or "
                          "remove it on every service")
                else:
                    print("could not revoke the shared GOOGLE_REFRESH_TOKEN; delete the "
                          "OAuth client at Google and replace or remove the variable")
            if report.shared_moodle:
                print("MOODLE_TOKEN is configured as a shared fallback; it is not revoked "
                      "here — delete it in Moodle and replace or remove it")
            if report.moodle_cleared and not args.dry_run:
                print(f"{report.moodle_cleared} Moodle token(s) were forgotten here but are "
                      "still valid at Moodle; delete them in Moodle: Site administration > "
                      "Server > Web services > Manage tokens")
            if report.moodle_kept:
                print(f"{report.moodle_kept} stored Moodle token(s) were left in place "
                      "(--keep-moodle); they stay live until deleted in Moodle")
            if report.google_acknowledged:
                print(f"--google-revoked: cleared {report.google_acknowledged} record(s) "
                      "of Google grants that could not be revoked here; taking your word "
                      "that they were removed at Google")
            if report.moodle_acknowledged:
                print(f"--moodle-revoked: cleared {report.moodle_acknowledged} record(s) "
                      "of Moodle tokens; taking your word that they were deleted in Moodle")
            # Automation must not read a half-done lockdown as success, and a
            # retry must not either: the tokens are gone after the first run,
            # so what is still live is read from the records it left
            # (outstanding_grants), not from this run's counts. They go only
            # on a successful revocation or an explicit acknowledgement; the
            # env-var fallbacks have no records because the variable is still
            # set on every run.
            for provider, email, reason in report.outstanding:
                print(f"  outstanding: {provider:6} {email} ({reason})")
            shared_moodle_live = report.shared_moodle and not args.moodle_revoked
            shared_google_live = (report.shared_google and not report.shared_google_revoked
                                  and not args.google_revoked)
            incomplete = (report.google_outstanding or report.moodle_outstanding
                          or shared_moodle_live or shared_google_live)
            if incomplete and not args.dry_run:
                print("lockdown is NOT complete: some grants may still be live. Remove "
                      "them (OAuth client at Google; tokens in Moodle), then re-run "
                      "with --google-revoked / --moodle-revoked to confirm")
                raise SystemExit(1)
            return
        user = find_user(session, args.email)
        if user is None:
            raise SystemExit(f"no such user {args.email} — log in via the web UI first")
        if args.command == "revoke-sessions":
            revoke_sessions(session, user)
            print(f"signed {user.email} out of every session")
            return
        if args.command == "delete-user":
            email = user.email
            try:
                revoked = delete_account(session, user)
            except SyncRunning:
                raise SystemExit(f"a sync for {email} is running; nothing deleted, "
                                 "try again in a few minutes") from None
            print(f"deleted {email} and all their data")
            if not revoked:
                print("could not reach Google to revoke their grant; they can remove "
                      "it at myaccount.google.com/permissions")
            return
        claimed, skipped = claim_unowned(session, user)
        print(f"assigned {claimed} unowned course(s) to {user.email}")
        if skipped:
            print(f"skipped {skipped} they already have their own copy of")


if __name__ == "__main__":
    main()
