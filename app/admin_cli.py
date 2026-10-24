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
            )
            if args.dry_run:
                print(f"dry run: would sign out every user and forget credentials "
                      f"for {report.cleared} of them")
            else:
                print(f"signed out every user; forgot credentials for {report.cleared}")
                print(f"google grants revoked: {report.google_revoked}"
                      + (f", could not revoke: {report.google_failed}"
                         if report.google_failed else ""))
            if report.undecryptable:
                print(f"{report.undecryptable} stored Google token(s) did not decrypt "
                      "(SECRET_KEY already rotated?); delete the OAuth client at Google "
                      "to kill the grants behind them")
            if report.shared_google:
                if args.dry_run:
                    print("GOOGLE_REFRESH_TOKEN is configured as a shared fallback; dry "
                          "run: would revoke it with Google, then replace or remove it")
                elif args.no_google_revoke:
                    print("GOOGLE_REFRESH_TOKEN is configured as a shared fallback and was "
                          "left untouched (--no-google-revoke); revoke it or delete the "
                          "OAuth client, then replace or remove it")
                elif report.shared_google_revoked:
                    print("revoked the shared GOOGLE_REFRESH_TOKEN with Google; replace or "
                          "remove it on every service")
                else:
                    print("could not revoke the shared GOOGLE_REFRESH_TOKEN; delete the "
                          "OAuth client at Google and replace or remove the variable")
            if report.shared_moodle:
                print("MOODLE_TOKEN is configured as a shared fallback; it is not revoked "
                      "here — delete it in Moodle and replace or remove it")
            if not args.keep_moodle and not args.dry_run:
                print("Moodle tokens were forgotten here but are still valid at Moodle; "
                      "delete them in Moodle: Site administration > Server > "
                      "Web services > Manage tokens")
            # automation must not read a half-done lockdown as success: a grant
            # Google was not asked about, or could not revoke, is still live
            incomplete = report.google_failed or (
                report.shared_google and not report.shared_google_revoked)
            if incomplete and not args.dry_run:
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
